from opendbc.can.packer import CANPacker
from common.realtime import DT_CTRL
from selfdrive.car import apply_toyota_steer_torque_limits
from selfdrive.car.chrysler.chryslercan import create_lkas_hud, create_lkas_command, create_cruise_buttons, acc_command, create_acc_1_message, create_das_4_message, create_chime_message#, create_lkas_heartbit
from selfdrive.car.chrysler.values import CAR, RAM_CARS, RAM_DT, RAM_HD, CarControllerParams
from cereal import car
from common.numpy_fast import clip
from common.conversions import Conversions as CV
from common.params import Params, put_nonblocking
import math

from common.op_params import opParams

ButtonType = car.CarState.ButtonEvent.Type
LongCtrlState = car.CarControl.Actuators.LongControlState
# braking
BRAKE_CHANGE = 0.06

GearShifter = car.CarState.GearShifter

class CarController:
  def __init__(self, dbc_name, CP, VM):
    self.CP = CP
    self.apply_steer_last = 0
    self.frame = 0

    self.hud_count = 0
    self.last_lkas_falling_edge = 0
    self.lkas_control_bit_prev = False
    self.last_button_frame = 0

    self.packer = CANPacker(dbc_name)
    self.params = CarControllerParams(CP)

    # long
    self.last_brake = None
    self.max_gear = 8
    self.op_params = opParams()
    self.desired_velocity = 0
    self.calc_velocity = 0
    self.last_standstill = 0

  def update(self, CC, CS):
    can_sends = []
    if self.CP.carFingerprint in RAM_CARS:
      lkas_active = CC.latActive and not CS.lkasdisabled
    else: lkas_active = CC.latActive and self.lkas_control_bit_prev and CC.enabled

    # cruise buttons
    if self.CP.carFingerprint in RAM_CARS:
      if (CS.button_counter != self.last_button_frame):
        das_bus = 2 if self.CP.carFingerprint in RAM_CARS else 0
        self.last_button_frame = CS.button_counter
        if self.CP.carFingerprint in RAM_CARS:
          if CS.cruise_cancel:
            can_sends.append(create_cruise_buttons(self.packer, CS.button_counter, das_bus, CS.cruise_buttons, cancel=True))
          else:
            can_sends.append(create_cruise_buttons(self.packer, CS.button_counter, das_bus, CS.cruise_buttons, cancel=CC.cruiseControl.cancel, resume=CC.cruiseControl.resume))

        # ACC cancellation
        elif CC.cruiseControl.cancel:
          can_sends.append(create_cruise_buttons(self.packer, CS.button_counter+1, das_bus, CS.cruise_buttons, cancel=True))

        # ACC resume from standstill
        elif CC.cruiseControl.resume:
          can_sends.append(create_cruise_buttons(self.packer, CS.button_counter+1, das_bus, CS.cruise_buttons, resume=True))
    else:
      if CS.button_pressed(ButtonType.accOnOff, False):
        CS.longAvailable = not CS.longAvailable
        CS.longEnabled = False

      if CS.longAvailable:
        if CS.button_pressed(ButtonType.cancel) or CS.out.brakePressed:
          CS.longEnabled = False
        elif CS.button_pressed(ButtonType.accelCruise) or \
            CS.button_pressed(ButtonType.decelCruise) or \
            CS.button_pressed(ButtonType.resumeCruise):
          CS.longEnabled = True
    # steering
    if self.frame % 2 == 0:
      
      lkas_control_bit = self.lkas_control_bit_prev
      # TODO: can we make this more sane? why is it different for all the cars?
      if self.CP.carFingerprint in RAM_DT:
        if CS.out.vEgo >= self.CP.minEnableSpeed and CS.out.vEgo <= self.CP.minEnableSpeed + 0.5:
          lkas_control_bit = True
        if (self.CP.minEnableSpeed >= 14.5)  and (CS.out.gearShifter != GearShifter.drive) :
          lkas_control_bit = False
      elif CS.out.vEgo > self.CP.minSteerSpeed:
        lkas_control_bit = True
      elif self.CP.carFingerprint in (CAR.PACIFICA_2019_HYBRID, CAR.PACIFICA_2020, CAR.JEEP_CHEROKEE_2019):
        if CS.out.vEgo < (self.CP.minSteerSpeed - 3.0):
          lkas_control_bit = False
      elif self.CP.carFingerprint in RAM_HD:
        if CS.out.vEgo < (self.CP.minSteerSpeed - 0.5):
          lkas_control_bit = False

      # EPS faults if LKAS re-enables too quickly
      lkas_control_bit = lkas_control_bit and (self.frame - self.last_lkas_falling_edge > 200) and not CS.out.steerFaultTemporary and not CS.out.steerFaultPermanent

      if not lkas_control_bit and self.lkas_control_bit_prev:
        self.last_lkas_falling_edge = self.frame

      # steer torque
      new_steer = int(round(CC.actuators.steer * self.params.STEER_MAX))
      apply_steer = apply_toyota_steer_torque_limits(new_steer, self.apply_steer_last, CS.out.steeringTorqueEps, self.params)
      if not lkas_active or not lkas_control_bit or not self.lkas_control_bit_prev:
        apply_steer = 0
      self.apply_steer_last = apply_steer
      self.lkas_control_bit_prev = lkas_control_bit

      can_sends.append(create_lkas_command(self.packer, self.CP, int(apply_steer), lkas_control_bit))
      #LONG
      
      #calculating acceleration/torque
      self.accel = clip(CC.actuators.accel, CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX)

      brake_threshold = -self.op_params.get('brake_threshold') if CS.out.vEgo > 2.25 else 0
      #Braking
      if CC.actuators.accel < brake_threshold: 
      #stopping = CC.actuators.longControlState == car.CarControl.Actuators.LongControlState
      #if stopping:
        accel_req = 0
        decel_req = 1
        torque = 0
        decel = self.acc_brake(self.accel)
        max_gear = 8
        if (decel < -1.9 and decel > -2.1 and CS.out.vEgo == 0):
          stand_still = 1
          self.last_standstill = 1
        else: 
          stand_still = 0
          self.last_standstill = 0

      #Acclerating
      else:
        time_for_sample = self.op_params.get('long_time_constant')
        torque_limits = self.op_params.get('torque_limits')
        drivetrain_efficiency = self.op_params.get('drivetrain_efficiency')
        self.last_brake = None

        # delta_accel = CC.actuators.accel - CS.out.aEgo

        # distance_moved = ((delta_accel * time_for_sample**2)/2) + (CS.out.vEgo * time_for_sample)
        # torque = (self.CP.mass * delta_accel * distance_moved * time_for_sample)/((drivetrain_efficiency * CS.engineRpm * 2 * math.pi) / 60)

        # # force (N) = mass (kg) * acceleration (m/s^2)
        # force = self.CP.mass * delta_accel
        # # distance_moved (m) =  (acceleration(m/s^2) * time(s)^2 / 2) + velocity(m/s) * time(s)
        # distance_moved = ((delta_accel) * (time_for_sample**2))/2) + (CS.out.vEgo * time_for_sample)
        # # work (J) = force (N) * distance (m)
        # work = force * distance_moved
        # # Power (W)= work(J) * time (s)
        # power = work * time_for_sample
        # # torque = Power (W) / (RPM * 2 * pi / 60)
        # torque = power/((drivetrain_efficiency * CS.engineRpm * 2 * math.pi) / 60)
        self.calc_velocity = ((self.accel-CS.out.aEgo) * time_for_sample) + CS.out.vEgo
        if 1==2: #self.op_params.get('comma_speed'):
          self.desired_velocity = min(CC.actuators.accel, CS.out.cruiseState.speed)
        else:
          self.desired_velocity = min(self.calc_velocity, CS.out.cruiseState.speed)
        # kinetic energy (J) = 1/2 * mass (kg) * velocity (m/s)^2
        # use the kinetic energy from the desired velocity - the kinetic energy from the current velocity to get the change in velocity
        kinetic_energy = ((self.CP.mass * self.desired_velocity **2)/2) - ((self.CP.mass * CS.out.vEgo**2)/2)
        # convert kinetic energy to torque
        # torque(NM) = (kinetic energy (J) * 9.55414 (Nm/J) * time(s))/RPM
        torque = (kinetic_energy * 9.55414 * time_for_sample)/(drivetrain_efficiency * CS.engineRpm + 0.001)
        torque = clip(torque, -torque_limits, torque_limits) # clip torque to -6 to 6 Nm for sanity

        if CS.engineTorque < 0 and torque > 0:
          #If the engine is producing negative torque, we need to return to a reasonable torque value quickly.
          # rough estimate of external forces in N
          total_forces = 650
          #torque required to maintain speed
          torque = (total_forces * CS.out.vEgo * 9.55414)/(CS.engineRpm * drivetrain_efficiency + 0.001)

        #If torque is positive, add the engine torque to the torque we calculated. This is because the engine torque is the torque the engine is producing.
        else:
          torque += CS.engineTorque
        #Value for sending accleration
        accel_req = 1 #if self.last_standstill == 1 else 0
        decel_req = 0
        decel = 4
        max_gear = 9
        stand_still = 0
        self.last_standstill = 0
        
      #Pacifica 
      if self.CP.carFingerprint not in RAM_CARS:
        #When stepping on gas/brake or OP not enabled
        override_request = CS.out.gasPressed #or CS.out.brakePressed
        if override_request or not CS.longEnabled or not CS.out.cruiseState.enabled:
          self.last_brake = None
          self.last_standstill = 0
          decel_req = 0
          accel_req = 0
          torque = 0
          max_gear = 9
          decel = 0
          stand_still = 0

          can_sends.append(acc_command(self.packer, self.frame / 2, 0,
                             CS.out.cruiseState.available,
                             CS.out.cruiseState.enabled,
                             accel_req,
                             torque,
                             max_gear,
                             decel_req,
                             decel,
                             0, 1, stand_still))
          can_sends.append(acc_command(self.packer, self.frame / 2, 2,
                              CS.out.cruiseState.available,
                              CS.out.cruiseState.enabled,
                              accel_req,
                              torque,
                              max_gear,
                              decel_req,
                              decel,
                              0, 1, stand_still))
        # #OP not enabled  
        # elif not CS.longEnabled or not CS.out.cruiseState.enabled: 
        #   self.last_brake = None
        #   self.last_standstill = 0
        #   self.max_gear = 9
        #   decel_req = 0
        #   accel_req = 0
        #   torque = 0
        #   max_gear = 9
        #   decel = 0
        #   stand_still = 0

        #   can_sends.append(acc_command(self.packer, self.frame / 2, 0,
        #                      CS.out.cruiseState.available,
        #                      CS.out.cruiseState.enabled,
        #                      accel_req,
        #                      torque,
        #                      max_gear,
        #                      decel_req,
        #                      decel,
        #                      0, 1, stand_still))
        #   can_sends.append(acc_command(self.packer, self.frame / 2, 2,
        #                       CS.out.cruiseState.available,
        #                       CS.out.cruiseState.enabled,
        #                       accel_req,
        #                       torque,
        #                       max_gear,
        #                       decel_req,
        #                       decel,
        #                       0, 1, stand_still))

        else: #send acceleration torque calculated from above
          can_sends.append(acc_command(self.packer, self.frame / 2, 0,
                             CS.out.cruiseState.available,
                             CS.out.cruiseState.enabled,
                             accel_req,
                             torque,
                             max_gear,
                             decel_req,
                             decel,
                             0, 1, stand_still))
          can_sends.append(acc_command(self.packer, self.frame / 2, 2,
                              CS.out.cruiseState.available,
                              CS.out.cruiseState.enabled,
                              accel_req,
                              torque,
                              max_gear,
                              decel_req,
                              decel,
                              0, 1, stand_still))

        if self.frame % 2 == 0:
          can_sends.append(create_acc_1_message(self.packer, 0, self.frame / 2))
          can_sends.append(create_acc_1_message(self.packer, 2, self.frame / 2))

        # if self.frame % 10 == 0:
        #   new_msg = create_lkas_heartbit(self.packer, 0, CS.lkasHeartbit)
        #   can_sends.append(new_msg)

        if self.frame % 6 == 0:
          state = 0
          if CS.out.cruiseState.available:
            state = 2 if CS.out.cruiseState.enabled else 1 #1/2 for regular cc, 3/4 for ACC
          can_sends.append(create_das_4_message(self.packer, 0, state, CC.hudControl.setSpeed)) #need to double check setSpeed
          can_sends.append(create_das_4_message(self.packer, 2, state, CC.hudControl.setSpeed))

        if self.frame % 50 == 0:
          # tester present - w/ no response (keeps radar disabled)
          can_sends.append((0x753, 0, b"\x02\x3E\x80\x00\x00\x00\x00\x00", 0))
          
        if self.frame % 100 == 0:
          can_sends.append(create_chime_message(self.packer, 0))
          can_sends.append(create_chime_message(self.packer, 2))
          
      else: 
        das_3_counter = CS.das_3['COUNTER']
        can_sends.append(acc_command(self.packer, das_3_counter, 0, 
                                    1, 
                                    CC.enabled,
                                    accel_req,
                                    torque,
                                    max_gear,
                                    decel_req,
                                    decel,
                                    CS.das_3))

    # HUD alerts
    if self.frame % 25 == 0:
      if CS.lkas_car_model != -1:
        can_sends.append(create_lkas_hud(self.packer, self.CP, lkas_active, CC.hudControl.visualAlert, self.hud_count, CS.lkas_car_model, CS))
        self.hud_count += 1

    self.frame += 1

    new_actuators = CC.actuators.copy()
    new_actuators.steer = self.apply_steer_last / self.params.STEER_MAX

    return new_actuators, can_sends

  def acc_brake(self, aTarget):
    brake_target = aTarget
    if self.last_brake is None:
      self.last_brake = min(0., brake_target / 2)
    else:
      tBrake = brake_target
      lBrake = self.last_brake
      if tBrake < lBrake:
        diff = min(BRAKE_CHANGE, (lBrake - tBrake) / 2)
        self.last_brake = max(lBrake - diff, tBrake)
      elif tBrake - lBrake > 0.01:  # don't let up unless it's a big enough jump
        diff = min(BRAKE_CHANGE, (tBrake - lBrake) / 2)
        self.last_brake = min(lBrake + diff, tBrake)
    return self.last_brake
