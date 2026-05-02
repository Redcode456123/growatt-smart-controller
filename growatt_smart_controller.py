#!/usr/bin/env python3
"""
Growatt SPF 6000ES Plus Lebanon Controller - v6.0 COMPLETE
Handles 4-hour random grid with predictive solar forecasting.
Includes: Horizon planning, grid rarity tracking, full API caching,
EEPROM protection, and all production improvements.

Register map source: Growatt OffGrid Modbus Protocol V0.14 (2021-04-20)
confirmed against real-world SPF 6000 ES Plus modbus.yaml community implementation.

KEY REGISTER CORRECTIONS:
  INPUT REGISTERS (function code 04):
    System status  : 0      (was 100)
    Fault code     : 40     (was 115)
    PV1 power      : 3+4   uint32 ×0.1W
    Battery voltage: 17    ×0.01V
    Battery SOC    : 18
    Grid voltage   : 20    ×0.1V
    Grid frequency : 21    ×0.01Hz
    BMS SOC        : 203
    Battery power  : 77    int32 ×0.1W (signed)

  HOLDING REGISTERS (function code 03 read / 06 write):
    Standby On/Off         : 0
    Output Source Priority : 1   (0=UTI, 1=SBU, 2=SUB)
    Charger Source Priority: 2   (0=CSO, 1=CUE, 2=OSO)  ← FIXED was 14!
"""

import minimalmodbus
import requests
import sqlite3
import time
import logging
import glob
import os
import signal
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional, List
import threading
# ──────────────────────────────────────────────────────────────────────────────
# LOGGING SETUP
# ──────────────────────────────────────────────────────────────────────────────
LOG_DIR = '/var/log'
if not os.path.exists(LOG_DIR):
    LOG_DIR = '/tmp'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'{LOG_DIR}/growatt_controller.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────
class Config:
    # System
    LATITUDE = 33.87
    LONGITUDE = 35.50
    TIMEZONE = "Asia/Beirut"

    # Solar & Battery
    PANEL_PEAK_REAL = 3040        # Watts (your actual panel peak)
    INVERTER_EFF = 0.85           # Conservative: accounts for dust, cable losses
    BATTERY_CAPACITY = 10.0       # kWh
    AVG_DAILY_USAGE = 5.6         # kWh/day
    HOME_USAGE_WATTS = 233        # Average watts (5.6 kWh/day)

    # Horizon planning
    HORIZON_HOURS = 12            # Look ahead 12 hours for planning
    DEFICIT_SEVERE_THRESHOLD = -2.0  # kWh - severe deficit threshold
    DEFICIT_OPPORTUNITY_THRESHOLD = -1.0  # kWh - opportunistic charge threshold

    # Grid rarity tracking
    GRID_RARITY_THRESHOLD_HOURS = 6  # Grid is "rare" if appearing every 6+ hours
    GRID_RARITY_SAMPLE_SIZE = 10    # Track last 10 appearances

    # Grid stability (voltage hysteresis)
    GRID_STABILITY_DELAY_SEC = 120        # Wait 2 min before trusting grid
    GRID_STABILITY_MIN_VOLTAGE = 190      # V — reject below (brownout)
    GRID_STABILITY_MAX_VOLTAGE = 260      # V — reject above (over-voltage)
    GRID_STABILITY_RECOVER_VOLTAGE = 255  # V — re-accept only below this after over-voltage
    GRID_STABILITY_MIN_FREQ = 49.0        # Hz
    GRID_STABILITY_MAX_FREQ = 51.0        # Hz

    # Battery hysteresis thresholds (prevents toggling)
    BATTERY_CRITICAL_THRESHOLD = 20   # % — enter critical mode below this
    BATTERY_CRITICAL_RECOVERY = 30    # % — leave critical mode only above this
    BATTERY_LOW_THRESHOLD = 40        # % — enter low mode below this
    BATTERY_LOW_RECOVERY = 50         # % — leave low mode only above this

    # Solar thresholds
    SOLAR_STRONG_THRESHOLD = 1500   # W
    SOLAR_WEAK_THRESHOLD = 500      # W

    # Forecast
    DEFAULT_FORECAST_KWH = 3.5      # Conservative default for Lebanon winter
    FORECAST_CACHE_HOURS = 6
    FORECAST_DECAY_START_HOURS = 24  # Start decaying after 24 hours
    FORECAST_DECAY_MAX_HOURS = 48    # Decay to 50% after 48 hours

    SPIKE_THRESHOLD_W = 800        # sudden jump above average
    HEAVY_LOAD_W = 1500           # absolute heavy load

    # Fast spike detection
    SPIKE_FAST_INTERVAL = 2        # seconds
    SPIKE_EMA_ALPHA = 0.2          # smoothing factor
    SPIKE_DELTA_W = 700            # jump above baseline
    SPIKE_ABSOLUTE_W = 1800        # absolute heavy load
    SPIKE_HOLD_SECONDS = 180       # keep spike active
    # ──────────────────────────────────────────────────────────────────────────
    # MODBUS — SPF 6000 ES Plus - CORRECTED REGISTERS
    # Protocol: Growatt OffGrid V0.14 (2021-04-20)
    # ──────────────────────────────────────────────────────────────────────────
    MODBUS_PORT = '/dev/ttyXRUSB*'
    MODBUS_SLAVE_ID = 1
    MODBUS_BAUDRATE = 9600
    MODBUS_TIMEOUT = 2
    MODBUS_RETRY_COUNT = 3
    MODBUS_RETRY_DELAY = 5

    # INPUT REGISTERS (read with function code 04)
    REG_INPUT_SYSTEM_STATUS = 0      # 0=standby, 1=normal, 2=fault, 3=absent
    REG_INPUT_PV1_VOLT = 1           # ×0.1 V
    REG_INPUT_PV2_VOLT = 2           # ×0.1 V
    REG_INPUT_PV1_PWR_H = 3          # 32-bit PV1 power HIGH word (×0.1W)
    REG_INPUT_PV2_PWR_H = 5          # 32-bit PV2 power HIGH word (×0.1W)
    REG_INPUT_BAT_VOLTAGE = 17       # ×0.01 V
    REG_INPUT_BAT_SOC = 18           # integer %
    REG_INPUT_GRID_VOLTAGE = 20      # ×0.1 V
    REG_INPUT_GRID_FREQ = 21         # ×0.01 Hz
    REG_INPUT_AC_POWER_H = 36        # 32-bit AC input power HIGH word (×0.1W)
    REG_INPUT_FAULT_CODE = 40
    REG_INPUT_BAT_POWER = 77         # int32 ×0.1 W (signed: +discharge, -charge)
    REG_INPUT_BMS_SOC = 203          # integer % (if BMS comms active)
    REG_INPUT_BMS_VOLT = 204         # ×0.01 V
    REG_INPUT_OUTPUT_POWER_H = 9

    # HOLDING REGISTERS (read FC03 / write FC06) - EEPROM!
    REG_HOLD_STANDBY = 0
    REG_HOLD_OUTPUT_PRIORITY = 1     # 0=UTI, 1=SBU, 2=SUB
    REG_HOLD_CHARGER_PRIORITY = 2    # 0=CSO, 1=CUE, 2=OSO (FIXED: was 14!)

    # Output priority encoding
    OUTPUT_UTI = 0   # Utility First
    OUTPUT_SBU = 1   # Solar → Battery → Utility
    OUTPUT_SUB = 2   # Solar → Utility → Battery

    # Charger priority encoding (V0.14)
    CHARGER_CSO = 0  # PV Only - solar only charging
    CHARGER_CUE = 1  # PV + Utility - both simultaneously
    CHARGER_OSO = 2  # PV Priority - solar first, grid backup

    # Polling
    POLL_INTERVAL_SEC = 300
    FORCE_SYNC_INTERVAL_HOURS = 24

    # EEPROM protection
    WRITE_THROTTLE_SEC = 60
    MAX_WRITES_PER_DAY = 50

    # Database
    DB_PATH = '/home/pi/growatt_history.db'
    DB_RETENTION_DAYS = 30
    DB_JOURNAL_MODE = 'WAL'

    # Safety
    DRY_RUN = True
    DRY_RUN_AUTO_DISABLE_HOURS = 48
    
    NO_EXPORT = True


# ──────────────────────────────────────────────────────────────────────────────
# GRID RARITY TRACKER
# ──────────────────────────────────────────────────────────────────────────────
class GridRarityTracker:
    def __init__(self, sample_size: int = 10):
        self.appearances: List[datetime] = []
        self.sample_size = sample_size

    def record_appearance(self):
        """Record a grid appearance event"""
        self.appearances.append(datetime.now())
        self.appearances = self.appearances[-self.sample_size:]

    def get_rarity_score(self) -> float:
        """
        Returns 0-1: 1 = very rare, 0 = very common
        Based on average hours between appearances
        """
        if len(self.appearances) < 2:
            return 0.5

        intervals = []
        for i in range(1, len(self.appearances)):
            interval = (self.appearances[i] - self.appearances[i-1]).total_seconds() / 3600
            intervals.append(interval)

        avg_interval = sum(intervals) / len(intervals)
        rarity = min(1.0, avg_interval / Config.GRID_RARITY_THRESHOLD_HOURS)
        return rarity

    def should_opportunistic_charge(self) -> bool:
        """Determine if grid is rare enough to charge opportunistically"""
        return self.get_rarity_score() > 0.7


# ──────────────────────────────────────────────────────────────────────────────
# FORECAST CACHE WITH GRADUAL DECAY
# ──────────────────────────────────────────────────────────────────────────────
class ForecastCache:
    def __init__(self, config: Config):
        self.config = config
        self.cached_forecast: Optional[Tuple[float, int, int]] = None
        self.last_successful_time: Optional[datetime] = None
        self._full_cache: Optional[Dict] = None
        self._full_cache_time: Optional[datetime] = None

    def get_forecast(self, fetcher_func) -> Tuple[float, int, int]:
        """Get forecast with fallback and gradual decay"""
        try:
            kwh, peak, hours = fetcher_func()
            if kwh > 0:
                self.cached_forecast = (kwh, peak, hours)
                self.last_successful_time = datetime.now()
                return kwh, peak, hours
            return self._cached_or_default()
        except Exception as e:
            logger.error(f"Forecast fetch failed: {e}")
            return self._cached_or_default()

    def _cached_or_default(self) -> Tuple[float, int, int]:
        """Return cached forecast with gradual decay, or default"""
        if self.cached_forecast and self.last_successful_time:
            age_h = (datetime.now() - self.last_successful_time).total_seconds() / 3600

            if age_h < self.config.FORECAST_CACHE_HOURS:
                return self.cached_forecast
            elif age_h < self.config.FORECAST_DECAY_MAX_HOURS:
                decay_start = self.config.FORECAST_DECAY_START_HOURS
                decay_max = self.config.FORECAST_DECAY_MAX_HOURS
                decay_factor = max(0.5, 1.0 - ((age_h - decay_start) / (decay_max - decay_start)))
                decayed_kwh = self.cached_forecast[0] * decay_factor
                logger.warning(f"Using decayed forecast: {decayed_kwh:.1f}kWh (age={age_h:.1f}h)")
                return (decayed_kwh, self.cached_forecast[1], self.cached_forecast[2])

        logger.warning(f"Using default forecast: {self.config.DEFAULT_FORECAST_KWH}kWh")
        return self.config.DEFAULT_FORECAST_KWH, 0, 0

    def get_full_forecast(self, fetcher_func) -> Optional[Dict]:
        """Get and cache the full API response"""
        now = datetime.now()
        if (self._full_cache_time and
            (now - self._full_cache_time).total_seconds() < self.config.FORECAST_CACHE_HOURS * 3600):
            return self._full_cache

        try:
            result = fetcher_func()
            if result:
                self._full_cache = result
                self._full_cache_time = now
            return result
        except Exception as e:
            logger.error(f"Full forecast fetch failed: {e}")
            return self._full_cache


# ──────────────────────────────────────────────────────────────────────────────
# EEPROM WRITE THROTTLE
# ──────────────────────────────────────────────────────────────────────────────
class WriteThrottle:
    def __init__(self, min_interval_seconds: int = 60, max_writes_per_day: int = 50):
        self.min_interval = min_interval_seconds
        self.max_writes_per_day = max_writes_per_day
        self.last_write_time: Optional[datetime] = None
        self.write_count_today = 0
        self.last_write_date = None

    def can_write(self) -> Tuple[bool, str]:
        now = datetime.now()
        today = now.date()

        if self.last_write_date != today:
            self.write_count_today = 0
            self.last_write_date = today
            return True, "New day"

        if self.write_count_today >= self.max_writes_per_day:
            return False, f"Daily limit {self.max_writes_per_day}"

        if self.last_write_time:
            elapsed = (now - self.last_write_time).total_seconds()
            if elapsed < self.min_interval:
                return False, f"Throttled ({elapsed:.0f}s)"

        return True, "OK"

    def record_write(self):
        self.last_write_time = datetime.now()
        self.write_count_today += 1
        if self.write_count_today % 10 == 0:
            logger.warning(f"Write count: {self.write_count_today}/{self.max_writes_per_day}")


# ──────────────────────────────────────────────────────────────────────────────
# MODBUS MANAGER - COMPLETE WITH ALL METHODS
# ──────────────────────────────────────────────────────────────────────────────
class ModbusManager:
    def __init__(self, config: Config):
        self.config = config
        self.instrument: Optional[minimalmodbus.Instrument] = None
        self.active_port: Optional[str] = None
        self.was_ever_connected = False
        self._lock = threading.Lock() 
        self.connect()

    def connect(self, is_reconnect: bool = False) -> bool:
        """Establish Modbus connection with auto-detection"""
        candidates = []

        user_port = self.config.MODBUS_PORT
        if user_port:
            if '*' in user_port:
                candidates.extend(glob.glob(user_port))
            elif os.path.exists(user_port):
                candidates.append(user_port)

        candidates.extend(glob.glob('/dev/ttyXRUSB*'))
        candidates.extend(glob.glob('/dev/ttyUSB*'))
        candidates.extend(glob.glob('/dev/ttyACM*'))

        ports_to_try = list(dict.fromkeys(candidates))

        if not ports_to_try:
            logger.error("❌ No serial ports found")
            return False

        for port in ports_to_try:
            time.sleep(0.05)

            for attempt in range(self.config.MODBUS_RETRY_COUNT):
                try:
                    if self.instrument:
                        try:
                            self.instrument.serial.close()
                        except:
                            pass

                    instr = minimalmodbus.Instrument(port, self.config.MODBUS_SLAVE_ID)
                    instr.serial.baudrate = self.config.MODBUS_BAUDRATE
                    instr.serial.bytesize = 8
                    instr.serial.parity = 'N'
                    instr.serial.stopbits = 1
                    instr.serial.timeout = self.config.MODBUS_TIMEOUT

                    test_val = instr.read_register(0, 0, 4)

                    if test_val in [0, 1, 2, 3]:
                        self.instrument = instr
                        self.active_port = port

                        if is_reconnect:
                            logger.info(f"✅ Reconnected on {port}")
                        elif self.was_ever_connected:
                            logger.info(f"✅ Re-established on {port}")
                        else:
                            logger.info(f"✅ Modbus connected on {port}")

                        self.was_ever_connected = True
                        return True

                except Exception as e:
                    logger.debug(f"Port {port} attempt {attempt + 1} failed: {e}")
                    continue

        logger.error("❌ No valid Modbus device found")
        return False

    def ensure_connected(self) -> bool:
        """Ensure connection is live, reconnect if needed. Lock-free (called before lock is acquired)."""
        try:
            if self.instrument:
                # Bare check without lock — intentionally not using read_input_register()
                self.instrument.serial.inWaiting  # cheap property, just checks serial is open
                return True
        except Exception:
            pass
    
        logger.warning("Modbus lost, reconnecting...")
        time.sleep(1)
        return self.connect(is_reconnect=True)

    def read_input_register(self, register: int, signed: bool = False) -> int:
        """Read 16-bit input register (FC04)"""
        if not self.ensure_connected():
            raise ConnectionError("Modbus not connected")
        with self._lock:
            for attempt in range(3):
                try:
                    return self.instrument.read_register(register, 0, 4, signed)
                except Exception as e:
                    if attempt == 2:
                        raise
                    time.sleep(0.5)
    
    def read_input_registers(self, start: int, count: int) -> list:
        """Read multiple input registers (FC04)"""
        if not self.ensure_connected():
            raise ConnectionError("Modbus not connected")
        with self._lock:
            for attempt in range(3):
                try:
                    return self.instrument.read_registers(start, count, 4)
                except Exception as e:
                    if attempt == 2:
                        raise
                    time.sleep(0.5)
    
    def read_holding_register(self, register: int) -> int:
        """Read holding register (FC03)"""
        if not self.ensure_connected():
            raise ConnectionError("Modbus not connected")
        with self._lock:
            return self.instrument.read_register(register, 0, 3)
    
    def write_holding_register(self, register: int, value: int) -> bool:
        """Write holding register (FC06) - EEPROM write!"""
        if self.config.DRY_RUN:
            logger.info(f"🔸 DRY RUN: Would write {value} to reg {register}")
            return True
        if not self.ensure_connected():
            return False
        with self._lock:
            try:
                self.instrument.write_register(register, value, 0, 6)
                logger.info(f"✍️ Wrote {value} to reg {register}")
                return True
            except Exception as e:
                logger.error(f"Write failed: {e}")
                return False

    def read_32bit_unsigned(self, start_reg: int) -> int:
        """Read 32-bit unsigned value from two consecutive input registers"""
        regs = self.read_input_registers(start_reg, 2)
        return (regs[0] << 16) | regs[1]

    def read_32bit_signed(self, start_reg: int) -> int:
        """
        Read 32-bit SIGNED value from two consecutive input registers.
        Used for battery power (Register 77) which can be negative.
        """
        regs = self.read_input_registers(start_reg, 2)
        raw = (regs[0] << 16) | regs[1]

        if raw & 0x80000000:
            raw = raw - 0x100000000

        return raw

    def read_pv_power(self, pv_num: int = 1) -> int:
        """Read PV power in Watts (unsigned)"""
        start_reg = self.config.REG_INPUT_PV1_PWR_H if pv_num == 1 else self.config.REG_INPUT_PV2_PWR_H
        try:
            raw = self.read_32bit_unsigned(start_reg)
            return int(raw / 10.0)
        except Exception as e:
            logger.error(f"PV{pv_num} power read failed: {e}")
            return 0

    def read_total_pv_power(self) -> int:
        """Read total PV power in Watts"""
        return self.read_pv_power(1) + self.read_pv_power(2)

    def read_battery_power(self) -> int:
        """
        Read battery power in Watts.
        Positive = discharging, Negative = charging.
        """
        try:
            raw = self.read_32bit_signed(self.config.REG_INPUT_BAT_POWER)
            return int(raw / 10.0)
        except Exception as e:
            logger.error(f"Battery power read failed: {e}")
            return 0

    def read_pv_voltage(self, pv_num: int = 1) -> float:
        """Read PV voltage in Volts"""
        reg = self.config.REG_INPUT_PV1_VOLT if pv_num == 1 else self.config.REG_INPUT_PV2_VOLT
        try:
            return self.read_input_register(reg) / 10.0
        except Exception as e:
            logger.error(f"PV{pv_num} voltage read failed: {e}")
            return 0.0

    def read_battery_voltage(self) -> float:
        """Read battery voltage in Volts"""
        try:
            return self.read_input_register(self.config.REG_INPUT_BAT_VOLTAGE) / 100.0
        except Exception as e:
            logger.error(f"Battery voltage read failed: {e}")
            return 0.0

    def read_bms_voltage(self) -> float:
        """Read BMS reported battery voltage (if available)"""
        try:
            return self.read_input_register(self.config.REG_INPUT_BMS_VOLT) / 100.0
        except Exception as e:
            logger.debug(f"BMS voltage read failed: {e}")
            return 0.0

    def read_bms_soc(self) -> int:
        """Read BMS reported SOC (if available)"""
        try:
            return self.read_input_register(self.config.REG_INPUT_BMS_SOC)
        except Exception as e:
            logger.debug(f"BMS SOC read failed: {e}")
            return -1

    def read_ac_power(self) -> int:
        """Read AC input power from grid in Watts"""
        try:
            raw = self.read_32bit_unsigned(self.config.REG_INPUT_AC_POWER_H)
            return int(raw / 10.0)
        except Exception as e:
            logger.error(f"AC power read failed: {e}")
            return 0


# ──────────────────────────────────────────────────────────────────────────────
# GRID MONITOR WITH VOLTAGE HYSTERESIS
# ──────────────────────────────────────────────────────────────────────────────
class GridMonitor:
    def __init__(self, modbus: ModbusManager, config: Config):
        self.modbus = modbus
        self.config = config
        self.stable_start_time: Optional[datetime] = None
        self.is_stable = False
        self.was_over_voltage = False
        self._high_voltage_warned = False
        self.last_spike_time = None
        self.in_spike_mode = False

    def _get_raw_grid_info(self) -> Dict:
        """Get raw grid status without stability filtering - COMPLETE"""
        try:
            run_state = self.modbus.read_input_register(self.config.REG_INPUT_SYSTEM_STATUS)
            voltage = self.modbus.read_input_register(self.config.REG_INPUT_GRID_VOLTAGE) / 10.0
            frequency = self.modbus.read_input_register(self.config.REG_INPUT_GRID_FREQ) / 100.0

            # Optional: Read grid power if needed
            try:
                grid_power_raw = self.modbus.read_ac_power()
            except:
                grid_power_raw = 0

            actual_grid_present = (voltage > 50.0 and frequency > 49.0)

            fault_code = 0
            if run_state == 2:
                try:
                    fault_code = self.modbus.read_input_register(self.config.REG_INPUT_FAULT_CODE)
                except:
                    pass

            state_map = {0: 'standby', 1: 'normal', 2: 'fault', 3: 'absent'}

            return {
                'present': (run_state == 1 and actual_grid_present),
                'state': state_map.get(run_state, 'unknown'),
                'voltage': voltage,
                'frequency': frequency,
                'fault_code': fault_code,
                'run_state_raw': run_state,
                'voltage_ok': voltage > 50,
                'grid_power_w': grid_power_raw,
            }
        except Exception as e:
            logger.error(f"Grid status read failed: {e}")
            return {
                'present': False, 'state': 'error',
                'voltage': 0.0, 'frequency': 0.0,
                'fault_code': -1, 'run_state_raw': -1,
                'voltage_ok': False,
                'grid_power_w': 0,
            }

    def _voltage_acceptable(self, voltage: float) -> bool:
        """Voltage hysteresis: reject at 260V, accept again at 255V"""
        if self.was_over_voltage:
            ok = voltage <= self.config.GRID_STABILITY_RECOVER_VOLTAGE
            if ok:
                self.was_over_voltage = False
                logger.info(f"Grid voltage recovered: {voltage:.1f}V")
            return ok
        else:
            ok = voltage <= self.config.GRID_STABILITY_MAX_VOLTAGE
            if not ok:
                self.was_over_voltage = True
                logger.warning(f"Grid over-voltage: {voltage:.1f}V")
            return ok

    def _quality_ok(self, info: Dict) -> bool:
        """Check voltage and frequency quality"""
        v = info['voltage']
        f = info['frequency']

        voltage_ok = (self._voltage_acceptable(v) and
                     v >= self.config.GRID_STABILITY_MIN_VOLTAGE)
        freq_ok = (self.config.GRID_STABILITY_MIN_FREQ <= f <=
                  self.config.GRID_STABILITY_MAX_FREQ)

        if v > 255 and not self._high_voltage_warned and not self.was_over_voltage:
            logger.warning(f"High grid voltage: {v:.1f}V")
            self._high_voltage_warned = True
        elif v <= 255:
            self._high_voltage_warned = False

        return voltage_ok and freq_ok

    def get_stable_grid_status(self) -> Dict:
        """Get grid status with stability delay and quality checks"""
        raw = self._get_raw_grid_info()
        now = datetime.now()
        quality_ok = raw['present'] and self._quality_ok(raw)

        if quality_ok:
            if self.stable_start_time is None:
                self.stable_start_time = now
                self.is_stable = False
                logger.info(f"Grid detected, stabilizing for {self.config.GRID_STABILITY_DELAY_SEC}s...")
            elif (now - self.stable_start_time).total_seconds() >= self.config.GRID_STABILITY_DELAY_SEC:
                if not self.is_stable:
                    self.is_stable = True
                    logger.info(f"✅ Grid stable: {raw['voltage']:.1f}V, {raw['frequency']:.2f}Hz")
            else:
                self.is_stable = False
        else:
            if self.stable_start_time is not None:
                logger.debug("Grid quality failed - resetting stability timer")
            self.stable_start_time = None
            self.is_stable = False

        stabilising = self.stable_start_time is not None and not self.is_stable

        return {
            'present': self.is_stable,
            'state': raw['state'] if self.is_stable else ('stabilising' if stabilising else raw['state']),
            'voltage': raw['voltage'],
            'frequency': raw['frequency'],
            'fault_code': raw['fault_code'],
            'stabilising': stabilising,
            'grid_power_w': raw['grid_power_w'],
        }



# ──────────────────────────────────────────────────────────────────────────────
# WEATHER FORECAST
# ──────────────────────────────────────────────────────────────────────────────
class WeatherForecast:
    def __init__(self, config: Config):
        self.config = config
        self.cache = ForecastCache(config)

    def _fetch_from_api(self) -> Optional[Dict]:
        """Fetch from Open-Meteo with exponential backoff and radiation sanity check."""
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": self.config.LATITUDE,
            "longitude": self.config.LONGITUDE,
            "timezone": self.config.TIMEZONE,
            "forecast_days": 3,
            "daily": [
                "weathercode", "sunrise", "sunset",
                "temperature_2m_max", "temperature_2m_min",
                "precipitation_sum", "sunshine_duration",
            ],
            "hourly": ["direct_radiation", "diffuse_radiation"],
        }
 
        MAX_RETRIES = 4
        BASE_DELAY  = 5   # seconds
 
        for attempt in range(MAX_RETRIES):
            try:
                r = requests.get(url, params=params, timeout=30)
                r.raise_for_status()
                data = r.json()
 
                # ── Sanity clamp (FIX 6) ──────────────────────────────────
                # Max physically plausible GHI at this latitude ≈ 1 100 W/m²
                MAX_GHI = 1_100.0
                hourly  = data.get("hourly", {})
                for key in ("direct_radiation", "diffuse_radiation"):
                    if key in hourly:
                        hourly[key] = [
                            min(v, MAX_GHI) if v is not None else 0
                            for v in hourly[key]
                        ]
 
                return data
 
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response else 0
                # Don't retry on client errors (4xx) except 429
                if 400 <= status < 500 and status != 429:
                    logger.error(f"API client error {status} — not retrying")
                    return None
                delay = BASE_DELAY * (2 ** attempt)
                logger.warning(f"API HTTP {status}, retry {attempt+1}/{MAX_RETRIES} in {delay}s")
                time.sleep(delay)
 
            except requests.exceptions.Timeout:
                delay = BASE_DELAY * (2 ** attempt)
                logger.warning(f"API timeout, retry {attempt+1}/{MAX_RETRIES} in {delay}s")
                time.sleep(delay)
 
            except Exception as e:
                logger.error(f"API unexpected error: {e}")
                return None
 
        logger.error("API failed after all retries")
        return None

    def _get_full_forecast(self) -> Optional[Dict]:
        """Get full forecast with caching"""
        return self.cache.get_full_forecast(self._fetch_from_api)

    def estimate_solar_yield(self, day_offset: int = 0) -> Tuple[float, int, int]:
        """Estimate daily solar yield in kWh"""
        def _calc():
            data = self._get_full_forecast()
            if not data:
                return 0.0, 0, 0

            target_date = (datetime.now() + timedelta(days=day_offset)).strftime("%Y-%m-%d")
            times = data["hourly"]["time"]
            direct = data["hourly"]["direct_radiation"]
            diffuse = data["hourly"]["diffuse_radiation"]

            daily_kwh = 0.0
            peak_w = 0
            productive_h = 0

            for i, t in enumerate(times):
                if not t.startswith(target_date):
                    continue
                ghi = (direct[i] or 0) + (diffuse[i] or 0)
                output_w = (ghi / 1000.0) * self.config.PANEL_PEAK_REAL * self.config.INVERTER_EFF
                daily_kwh += output_w / 1000.0
                if output_w > peak_w:
                    peak_w = output_w
                if output_w > 200:
                    productive_h += 1

            return daily_kwh, int(peak_w), productive_h

        return self.cache.get_forecast(_calc)

    def get_sunrise_today(self) -> datetime:
        from datetime import timezone
        data = self._get_full_forecast()
        fallback = datetime.now().replace(hour=6, minute=30, second=0, microsecond=0)
        
        
        try:
            if data and 'daily' in data and 'sunrise' in data['daily']:
                return datetime.fromisoformat(data['daily']['sunrise'][0]).replace(tzinfo=None)
        except Exception as e:
            logger.warning(f"Sunrise parse failed: {e}")
    
        return fallback
    
    
    def get_sunset_today(self) -> datetime:
        from datetime import timezone
        data = self._get_full_forecast()
        fallback = datetime.now().replace(hour=17, minute=30, second=0, microsecond=0)
        
        
        try:
            if data and 'daily' in data and 'sunset' in data['daily']:
                return datetime.fromisoformat(data['daily']['sunset'][0]).replace(tzinfo=None)
        except Exception as e:
            logger.warning(f"Sunset parse failed: {e}")
    
        return fallback

    def get_remaining_solar_kwh_today(self, current_solar_w: int = 0) -> float:
        """
        Remaining solar energy for the rest of today in kWh.
 
        SOLAR FALLBACK: if the API data is stale/missing AND we can see
        real solar production right now, use:
            remaining ≈ current_solar_w  ×  hours_until_sunset
        This prevents falsely pessimistic energy_balance when the cache
        has expired during a sunny afternoon.
        """
        data = self._get_full_forecast()
        now  = datetime.now()
 
        # ── Fallback: estimate from live solar reading ─────────────────────
        sunset   = self.get_sunset_today()
        hours_left = max(0.0, (sunset - now).total_seconds() / 3600.0)
        live_estimate = (current_solar_w / 1000.0) * hours_left * self.config.INVERTER_EFF
 
        if not data:
            if current_solar_w > 100:
                logger.warning(
                    f"No forecast data — using live solar fallback: "
                    f"{current_solar_w}W × {hours_left:.1f}h = {live_estimate:.2f}kWh"
                )
                return live_estimate
            return 0.0
 
        today_str     = now.strftime("%Y-%m-%d")
        times         = data["hourly"]["time"]
        direct        = data["hourly"]["direct_radiation"]
        diffuse       = data["hourly"]["diffuse_radiation"]
        remaining_kwh = 0.0
 
        for i, t in enumerate(times):
            if not t.startswith(today_str):
                continue
            try:
                hour_dt = datetime.fromisoformat(t)
            except ValueError:
                continue
            if hour_dt <= now:
                continue
            ghi     = (direct[i] or 0) + (diffuse[i] or 0)
            power_w = (ghi / 1000.0) * self.config.PANEL_PEAK_REAL * self.config.INVERTER_EFF
            remaining_kwh += power_w / 1000.0
 
        # Use whichever is larger: API forecast or live-sensor estimate
        # (protects against stale-API pessimism on a sunny day)
        result = max(remaining_kwh, live_estimate if current_solar_w > 200 else 0.0)
        return result
    
# ──────────────────────────────────────────────────────────────────────────────
# DATABASE MANAGER
# ──────────────────────────────────────────────────────────────────────────────
class DatabaseManager:
    def __init__(self, config: Config):
        self.config = config
        self.db_path = config.DB_PATH
        self._init()

    def _init(self):
        """Initialize database with indexes"""
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        conn.execute(f"PRAGMA journal_mode={self.config.DB_JOURNAL_MODE}")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=10000")
        conn.execute("PRAGMA temp_store=MEMORY")

        conn.execute('''
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                grid_state TEXT,
                grid_stable BOOLEAN,
                battery_soc REAL,
                solar_w INTEGER,
                forecast_kwh REAL,
                output_priority TEXT,
                charger_priority TEXT,
                output_changed BOOLEAN,
                charger_changed BOOLEAN,
                reason TEXT,
                energy_balance REAL
            )
        ''')

        conn.execute('''
            CREATE TABLE IF NOT EXISTS write_stats (
                date TEXT PRIMARY KEY,
                write_count INTEGER,
                output_changes INTEGER,
                charger_changes INTEGER
            )
        ''')

        conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON decisions(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_grid_state ON decisions(grid_state)")

        conn.commit()
        conn.close()
        self._cleanup()

    def _cleanup(self):
        """Delete old records"""
        try:
            cutoff = (datetime.now() - timedelta(days=self.config.DB_RETENTION_DAYS)).isoformat()
            conn = sqlite3.connect(self.db_path)
            n = conn.execute("DELETE FROM decisions WHERE timestamp < ?", (cutoff,)).rowcount
            conn.commit()
            conn.close()
            if n > 0:
                logger.info(f"Purged {n} old records")
        except Exception as e:
            logger.error(f"Cleanup failed: {e}")

    def log_decision(self, data: Dict):
        """Log a decision to database"""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute('''
                INSERT INTO decisions (
                    timestamp, grid_state, grid_stable, battery_soc, solar_w,
                    forecast_kwh, output_priority, charger_priority,
                    output_changed, charger_changed, reason, energy_balance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                data['timestamp'], data['grid_state'], data['grid_stable'],
                data['battery_soc'], data['solar_w'], data['forecast_kwh'],
                data['output_priority'], data['charger_priority'],
                data['output_changed'], data['charger_changed'],
                data['reason'], data.get('energy_balance', 0)
            ))
            conn.commit()
            conn.close()

            if os.path.getsize(self.db_path) > 10_000_000:
                self._cleanup()
        except Exception as e:
            logger.error(f"Log failed: {e}")

    def update_write_stats(self, output_changed: bool, charger_changed: bool):
        """Update daily write statistics"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            conn = sqlite3.connect(self.db_path)
            conn.execute('''
                INSERT INTO write_stats (date, write_count, output_changes, charger_changes)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    write_count = write_count + 1,
                    output_changes = output_changes + ?,
                    charger_changes = charger_changes + ?
            ''', (today,
                  1 if output_changed else 0,
                  1 if charger_changed else 0,
                  1 if output_changed else 0,
                  1 if charger_changed else 0))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Stats update failed: {e}")




class LogThrottle:
    def __init__(self):
        self.last = {}

    def allow(self, key: str, interval: int) -> bool:
        now = time.time()
        last = self.last.get(key, 0)
        if now - last >= interval:
            self.last[key] = now
            return True
        return False


# ──────────────────────────────────────────────────────────────────────────────
# MAIN CONTROLLER
# ──────────────────────────────────────────────────────────────────────────────
class GrowattController:
    OUTPUT_NAMES = {0: 'UTI', 1: 'SBU', 2: 'SUB'}
    OUTPUT_VALUES = {'UTI': 0, 'SBU': 1, 'SUB': 2}
    CHARGER_NAMES = {0: 'CSO', 1: 'CUE', 2: 'OSO'}
    CHARGER_VALUES = {'CSO': 0, 'CUE': 1, 'OSO': 2}
    

    def __init__(self):
        self.config = Config()
        self.log_throttle = LogThrottle()
        self.modbus = ModbusManager(self.config)
        self.grid = GridMonitor(self.modbus, self.config)
        self.weather = WeatherForecast(self.config)
        self.db = DatabaseManager(self.config)
        self.grid_rarity = GridRarityTracker()
        self.throttle = WriteThrottle(
            min_interval_seconds=self.config.WRITE_THROTTLE_SEC,
            max_writes_per_day=self.config.MAX_WRITES_PER_DAY,
        )

        self.current_output: Optional[str] = None
        self.current_charger: Optional[str] = None
        self.last_force_sync: Optional[datetime] = None
        self.last_grid_appearance: Optional[datetime] = None

        self._in_critical = False
        self._in_low = False

        self.dry_run_start = datetime.now()
        self.successful_polls = 0
        self.running = True
        
        # Spike detection (fast loop)
        # Spike detection (fast loop)
        self.spike_active = False
        self.spike_last_trigger: Optional[datetime] = None
        self._spike_lock = threading.Lock()   # FIX 3: protect cross-thread flag
 
        self.load_history: list = []           # FIX 2: owned by main thread only
        self.ema_load: Optional[float] = None  # owned by spike thread only

        # Start background spike monitor
        self._start_spike_monitor()

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        # Decision stickiness (FIX 5)
        self._last_decision_output:  Optional[str]      = None
        self._last_decision_charger: Optional[str]      = None
        self._decision_since:        Optional[datetime]  = None
        self.DECISION_MIN_HOLD_SEC = 600   # 10 minutes minimum before switching        

        self._read_current_settings()
        
        # ✅ ADD THIS LINE - Verify critical registers after reading settings
        if not self.verify_critical_registers():
            logger.error("⚠️ CRITICAL REGISTER VERIFICATION FAILED!")
            logger.error("   This may indicate a firmware update changed register mappings.")
            logger.error("   The script will continue but may misbehave.")
            logger.error("   Recommend running in DRY_RUN mode until verified.")
            if self.config.DRY_RUN:
                logger.info("   (DRY_RUN is enabled - no writes will occur)")
            else:
                logger.warning("   ⚠️ DRY_RUN is DISABLED - consider enabling for safety")
        
        
        
        

    def _signal_handler(self, sig, frame):
        logger.info("Shutdown signal received...")
        self.running = False


    def _apply_decision_stickiness(
        self,
        desired_output: str,
        desired_charger: str,
        urgency: str,
    ) -> Tuple[str, str, bool]:
        """
        Prevent rapid oscillation near thresholds (FIX 5).
 
        Rules:
          - Critical/high urgency decisions bypass stickiness immediately.
          - For medium/low urgency, the decision must differ from the current
            one for at least DECISION_MIN_HOLD_SEC before it is accepted.
 
        Returns:
            (effective_output, effective_charger, was_overridden)
            was_overridden=True means stickiness held the previous decision.
        """
        now = datetime.now()
 
        # Always honour urgent decisions immediately
        if urgency in ("critical", "high"):
            self._last_decision_output  = desired_output
            self._last_decision_charger = desired_charger
            self._decision_since        = now
            return desired_output, desired_charger, False
 
        # No previous decision recorded yet
        if self._last_decision_output is None:
            self._last_decision_output  = desired_output
            self._last_decision_charger = desired_charger
            self._decision_since        = now
            return desired_output, desired_charger, False
 
        # Decision unchanged — reset the clock
        if (desired_output == self._last_decision_output and
                desired_charger == self._last_decision_charger):
            self._decision_since = now
            return desired_output, desired_charger, False
 
        # Decision changed — check hold time
        held_sec = (now - self._decision_since).total_seconds() if self._decision_since else 0
        if held_sec < self.DECISION_MIN_HOLD_SEC:
            remaining = int(self.DECISION_MIN_HOLD_SEC - held_sec)
            logger.debug(
                f"Stickiness: holding {self._last_decision_output}/"
                f"{self._last_decision_charger} for {remaining}s more "
                f"(wanted {desired_output}/{desired_charger})"
            )
            return self._last_decision_output, self._last_decision_charger, True
 
        # Hold time elapsed — accept new decision
        self._last_decision_output  = desired_output
        self._last_decision_charger = desired_charger
        self._decision_since        = now
        return desired_output, desired_charger, False



    def verify_critical_registers(self) -> bool:
        """Verify critical registers are still valid after firmware update."""
        try:
            logger.info("🔍 Running critical register verification...")
            
            # Test reading critical input registers
            test_registers_input = [
                (self.config.REG_INPUT_SYSTEM_STATUS, "System Status", 0, 3),  # 0=standby,1=normal,2=fault,3=absent
                (self.config.REG_INPUT_BAT_SOC, "Battery SOC", 0, 100),
                (self.config.REG_INPUT_GRID_VOLTAGE, "Grid Voltage", 0, 3000),  # ×0.1V, so up to 300V
                (self.config.REG_INPUT_GRID_FREQ, "Grid Frequency", 0, 6000),   # ×0.01Hz, so up to 60Hz
                # Replace the BMS SOC entry in test_registers_input:
                # Remove this line entirely from the verification list:
                # (self.config.REG_INPUT_BMS_SOC, "BMS SOC", 0, 100),
                # BMS may not be connected - verified separately in _get_battery_soc()
            ]
            
            for reg, name, min_val, max_val in test_registers_input:
                val = self.modbus.read_input_register(reg)
                if val < min_val or val > max_val:
                    logger.error(f"❌ Input register {reg} ({name}) returned invalid value: {val}")
                    return False
                logger.debug(f"  Input reg {reg} ({name}) = {val} (valid)")
            
            # Test reading critical holding registers
            test_registers_holding = [
                (self.config.REG_HOLD_OUTPUT_PRIORITY, "Output Priority", 0, 2),  # 0=UTI,1=SBU,2=SUB
                (self.config.REG_HOLD_CHARGER_PRIORITY, "Charger Priority", 0, 2), # 0=CSO,1=CUE,2=OSO
            ]
            
            for reg, name, min_val, max_val in test_registers_holding:
                val = self.modbus.read_holding_register(reg)
                if val < min_val or val > max_val:
                    logger.error(f"❌ Holding register {reg} ({name}) returned invalid value: {val}")
                    return False
                logger.debug(f"  Holding reg {reg} ({name}) = {val} (valid)")
            
            logger.info("✅ Critical register verification PASSED")
            return True
            
        except Exception as e:
            logger.error(f"❌ Register verification FAILED: {e}")
            return False


    def _read_current_settings(self) -> bool:
        """Read current inverter settings"""
        try:
            v_out = self.modbus.read_holding_register(self.config.REG_HOLD_OUTPUT_PRIORITY)
            v_chr = self.modbus.read_holding_register(self.config.REG_HOLD_CHARGER_PRIORITY)
            self.current_output = self.OUTPUT_NAMES.get(v_out, f'UNKNOWN({v_out})')
            self.current_charger = self.CHARGER_NAMES.get(v_chr, f'UNKNOWN({v_chr})')
            logger.info(f"Current: Output={self.current_output}, Charger={self.current_charger}")
            return True
        except Exception as e:
            logger.error(f"Failed to read settings: {e}")
            return False
        
        
    def _update_load_history(self, solar_w: int) -> float:
        """
        Compute current house load, update rolling history, return average.
        Called once per poll cycle in make_decision(), before _energy_balance().
        Keeps all stateful load tracking in one place (FIX 2).
 
        Returns average load in Watts over the last ~30 min.
        """
        current_load_w = self._get_live_load_watts(solar_w)
        self.load_history.append(current_load_w)
        if len(self.load_history) > 6:   # 6 × 5 min = 30-min window
            self.load_history = self.load_history[-6:]
        avg = sum(self.load_history) / len(self.load_history)
        return avg        
        
        
        
        

    def _start_spike_monitor(self):
        """Background 2-second loop for fast load-spike detection.
 
        Runs in a daemon thread.  Only reads solar_w from the Modbus
        (through the existing Modbus lock) and writes self.spike_active
        under self._spike_lock.  Never touches load_history — that is
        main-thread state.
        """
        def loop():
            while self.running:
                try:
                    # Modbus read is already protected by ModbusManager._lock
                    solar_w = self._get_solar_watts()
 
                    # Quick load proxy: solar only (no battery/grid read
                    # to keep the fast loop cheap and contention-free)
                    load_w = float(solar_w)
 
                    if self.ema_load is None:
                        self.ema_load = load_w
 
                    alpha = self.config.SPIKE_EMA_ALPHA
                    self.ema_load = alpha * load_w + (1 - alpha) * self.ema_load
 
                    spike_detected = (
                        load_w > self.ema_load + self.config.SPIKE_DELTA_W or
                        load_w > self.config.SPIKE_ABSOLUTE_W
                    )
 
                    now = datetime.now()
 
                    with self._spike_lock:                    # FIX 3
                        if spike_detected:
                            if not self.spike_active:
                                if self.log_throttle.allow("spike", 30):
                                    logger.warning(
                                        f"⚡ SPIKE: {load_w:.0f}W "
                                        f"(EMA {self.ema_load:.0f}W)"
                                    )
                            self.spike_active = True
                            self.spike_last_trigger = now
                        elif self.spike_active and self.spike_last_trigger:
                            elapsed = (now - self.spike_last_trigger).total_seconds()
                            if elapsed > self.config.SPIKE_HOLD_SECONDS:
                                self.spike_active = False
 
                    time.sleep(self.config.SPIKE_FAST_INTERVAL)
 
                except Exception as e:
                    logger.error(f"Spike monitor error: {e}")
                    time.sleep(5)
 
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
 


    def _safe_write(self, desired_output: str, desired_charger: str) -> Tuple[bool, bool]:
        """Write settings only if changed and throttle allows"""
        need_output = desired_output is not None and desired_output != self.current_output
        need_charger = desired_charger is not None and desired_charger != self.current_charger

        if not need_output and not need_charger:
            return False, False

        ok, reason = self.throttle.can_write()
        if not ok:
            logger.debug(f"Write blocked: {reason}")
            return False, False

        out_changed = False
        chr_changed = False

        if need_output and desired_output in self.OUTPUT_VALUES:
            if self.modbus.write_holding_register(
                self.config.REG_HOLD_OUTPUT_PRIORITY, self.OUTPUT_VALUES[desired_output]):
                self.current_output = desired_output
                out_changed = True
                self.throttle.record_write()

        if need_charger and desired_charger in self.CHARGER_VALUES:
            if self.modbus.write_holding_register(
                self.config.REG_HOLD_CHARGER_PRIORITY, self.CHARGER_VALUES[desired_charger]):
                self.current_charger = desired_charger
                chr_changed = True
                self.throttle.record_write()

        if out_changed or chr_changed:
            self.db.update_write_stats(out_changed, chr_changed)

        return out_changed, chr_changed

    def _get_battery_soc(self) -> float:
        """Read battery SOC from BMS or inverter. Never returns None."""
        try:
            bms_soc = self.modbus.read_bms_soc()
            if 1 <= bms_soc <= 100:
                logger.debug(f"SOC from BMS: {bms_soc}%")
                return float(bms_soc)
        except Exception:
            pass
    
        try:
            soc = self.modbus.read_input_register(self.config.REG_INPUT_BAT_SOC)
            if 0 <= soc <= 100:
                logger.debug(f"SOC from inverter: {soc}%")
                return float(soc)
        except Exception as e:
            logger.error(f"SOC read failed: {e}")
    
        # Can't read SOC — return a safe conservative value and log loudly
        # Do NOT return None: all downstream logic assumes a float
        logger.error("❌ BATTERY SOC UNKNOWN — defaulting to 15% (conservative/critical)")
        return 15.0  # Forces critical/emergency path in decision logic



    def _get_solar_watts(self) -> int:
        """Read total solar power in Watts"""
        try:
            return self.modbus.read_total_pv_power()
        except Exception as e:
            logger.error(f"Solar read failed: {e}")
            return 0

    def _get_battery_power(self) -> int:
        """Read battery power in Watts (positive=discharge, negative=charge)"""
        try:
            return self.modbus.read_battery_power()
        except Exception as e:
            logger.error(f"Battery power read failed: {e}")
            return 0

    def _survival_hours(self, soc: float) -> float:
        """Hours battery can last at average load"""
        kwh_available = self.config.BATTERY_CAPACITY * (soc / 100.0)
        hourly_usage = self.config.AVG_DAILY_USAGE / 24.0
        return kwh_available / hourly_usage if hourly_usage > 0 else 0.0

    def _hours_until_sunrise(self) -> float:
        """Hours until next sunrise"""
        sunrise = self.weather.get_sunrise_today()
        now = datetime.now()
        if now < sunrise:
            return (sunrise - now).total_seconds() / 3600.0
        tomorrow = sunrise + timedelta(days=1)
        return (tomorrow - now).total_seconds() / 3600.0

    def _hours_until_sunset(self) -> float:
        """Hours until today's sunset"""
        sunset = self.weather.get_sunset_today()
        now = datetime.now()
        if now < sunset:
            return (sunset - now).total_seconds() / 3600.0
        return 0.0


    def _energy_balance(self, soc: float, solar_w: int, avg_load_w: float) -> float:
        """
        Horizon energy balance (kWh).
            positive = surplus
            negative = deficit
 
        FIX 1: called exactly once per poll cycle (caller passes avg_load_w).
        FIX 2: pure — no side effects, does not touch self.load_history.
 
        Args:
            soc         : current battery SOC (%)
            solar_w     : current PV output (W) — used as fallback for solar estimate
            avg_load_w  : rolling average load from main loop (W)
        """
        battery_kwh    = self.config.BATTERY_CAPACITY * (soc / 100.0)
        solar_remaining = self.weather.get_remaining_solar_kwh_today(solar_w)  # passes live W
        horizon_hours  = self.config.HORIZON_HOURS
        usage_kwh      = (avg_load_w / 1000.0) * horizon_hours
 
        return battery_kwh + solar_remaining - usage_kwh



    def _is_night(self, now: datetime) -> bool:
        """Determine if it's night using actual sunrise/sunset"""
        sunrise = self.weather.get_sunrise_today()
        sunset = self.weather.get_sunset_today()
        return now < sunrise or now > sunset

    def _check_dry_run(self):
        """Check if dry run period is complete"""
        if not self.config.DRY_RUN:
            return
        elapsed_h = (datetime.now() - self.dry_run_start).total_seconds() / 3600.0
        if elapsed_h >= self.config.DRY_RUN_AUTO_DISABLE_HOURS and self.successful_polls >= 10:
            logger.warning(f"✅ DRY RUN complete ({elapsed_h:.1f}h). Set DRY_RUN=False to enable.")

    def _alert(self, reason: str, output: str, charger: str, urgency: str):
        """Send alert for critical situations"""
        logger.warning(f"🚨 ALERT [{urgency}]: {reason}")
        logger.warning(f"   Action: Output={output}, Charger={charger}")

    def make_decision(self):
        """Main decision engine."""
        self._check_dry_run()
        now = datetime.now()
 
        # Periodic settings re-sync
        if (self.last_force_sync is None or
                (now - self.last_force_sync).total_seconds() >
                self.config.FORCE_SYNC_INTERVAL_HOURS * 3600):
            if self._read_current_settings():
                self.last_force_sync = now
 
        # ── Sensor reads ───────────────────────────────────────────────────
        grid_info    = self.grid.get_stable_grid_status()
        soc          = self._get_battery_soc()          # always a float
        solar_w      = self._get_solar_watts()
        forecast_kwh, _, _ = self.weather.estimate_solar_yield(0)
        sunrise      = self.weather.get_sunrise_today()
        sunset       = self.weather.get_sunset_today()
        time_to_sunset = max(0.0, (sunset - now).total_seconds() / 3600.0)
        pre_sunset_force = (time_to_sunset < 2.0 and soc < 70)
 
        # FIX 1+2: update load history once, then compute balance once
        avg_load_w   = self._update_load_history(solar_w)
        energy_balance = self._energy_balance(soc, solar_w, avg_load_w)   # single call
 
        # Grid rarity (FIX: transition-only recording — see grid rarity block)
        if grid_info['present']:
            if self.last_grid_appearance is None:
                self.grid_rarity.record_appearance()
                self.last_grid_appearance = now
            elif (now - self.last_grid_appearance).total_seconds() > 3600:
                self.grid_rarity.record_appearance()
                self.last_grid_appearance = now
        else:
            self.last_grid_appearance = None
 
        grid_rarity = self.grid_rarity.get_rarity_score()
 
        # ── Battery hysteresis ─────────────────────────────────────────────
        if self._in_critical:
            battery_critical = soc < self.config.BATTERY_CRITICAL_RECOVERY
            if not battery_critical:
                logger.info(f"Battery exited critical: {soc:.0f}%")
                self._in_critical = False
        else:
            battery_critical = soc < self.config.BATTERY_CRITICAL_THRESHOLD
            if battery_critical:
                logger.warning(f"Battery entered critical: {soc:.0f}%")
                self._in_critical = True
 
        if self._in_low:
            battery_low = soc < self.config.BATTERY_LOW_RECOVERY
            if not battery_low:
                logger.info(f"Battery exited low: {soc:.0f}%")
                self._in_low = False
        else:
            battery_low = (soc < self.config.BATTERY_LOW_THRESHOLD) and not battery_critical
            if battery_low:
                self._in_low = True
 
        battery_ok     = soc > 60
        battery_medium = 40 <= soc <= 60
        is_night       = self._is_night(now)
        is_late_day    = now > sunset - timedelta(hours=3)
        solar_strong   = solar_w > self.config.SOLAR_STRONG_THRESHOLD
        solar_weak     = solar_w < self.config.SOLAR_WEAK_THRESHOLD
        solar_medium   = not solar_strong and not solar_weak
        forecast_bad   = forecast_kwh < self.config.AVG_DAILY_USAGE
 
        # FIX 3: read spike flag under lock
        with self._spike_lock:
            spike_active = self.spike_active
 
        if self.log_throttle.allow("decision_summary", 300):
            logger.info(
                f"📊 Grid={grid_info['state']} V={grid_info['voltage']:.1f}V "
                f"Batt={soc:.0f}% Solar={solar_w}W Fcst={forecast_kwh:.1f}kWh "
                f"Balance={energy_balance:+.1f}kWh AvgLoad={avg_load_w:.0f}W "
                f"Rarity={grid_rarity:.0%} Spike={spike_active}"
            )
 
        # ── Decision tree ──────────────────────────────────────────────────
        desired_output  = self.current_output  or 'SBU'
        desired_charger = self.current_charger or 'CSO'
        reason  = ""
        urgency = "low"
 
        if grid_info.get('stabilising'):
            reason = f"⏳ Grid stabilizing ({self.config.GRID_STABILITY_DELAY_SEC}s)"
 
        elif grid_info['state'] == 'fault':
            desired_output  = 'SBU'
            desired_charger = 'CSO'
            reason  = f"⚠️ Grid fault (code {grid_info['fault_code']})"
            urgency = "high"
 
        elif grid_info['present']:
            # Priority order: safety → severe deficit → spike → pre-sunset
            # → opportunistic → capture → night → late-day → normal
 
            if battery_critical:
                desired_output  = 'UTI'
                desired_charger = 'CUE'
                reason  = f"🔴 EMERGENCY — Battery {soc:.0f}%!"
                urgency = "critical"
 
            elif energy_balance < self.config.DEFICIT_SEVERE_THRESHOLD:
                desired_output  = 'UTI'
                desired_charger = 'CUE'
                reason  = f"🔴 SEVERE DEFICIT: {energy_balance:+.1f}kWh"
                urgency = "high"
 
            elif spike_active:                               # FIX 3: local copy
                desired_output  = 'UTI'
                desired_charger = 'CUE'
                reason  = f"⚡ LOAD SPIKE — grid supporting battery"
                urgency = "high"
 
            elif pre_sunset_force:
                desired_output  = 'UTI'
                desired_charger = 'CUE'
                reason  = f"🌇 PRE-SUNSET CHARGE — {time_to_sunset:.1f}h left, SOC {soc:.0f}%"
                urgency = "high"
 
            elif (energy_balance < self.config.DEFICIT_OPPORTUNITY_THRESHOLD
                  and grid_rarity > 0.7):
                desired_output  = 'UTI'
                desired_charger = 'CUE'
                reason  = f"⚡ OPPORTUNISTIC — deficit {energy_balance:+.1f}kWh, rare grid"
                urgency = "medium"
 
            elif battery_low and forecast_bad:
                desired_output  = 'UTI'
                desired_charger = 'CUE'
                reason  = f"⚠️ CAPTURE GRID — batt {soc:.0f}%, bad forecast ({forecast_kwh:.1f}kWh)"
                urgency = "high"
 
            elif is_night and battery_low:
                desired_output  = 'UTI'
                desired_charger = 'CUE'
                reason  = "🌙 NIGHT GRID — charging battery"
                urgency = "medium"
 
            elif is_late_day and not battery_ok and solar_medium:
                desired_output  = 'SUB'
                desired_charger = 'OSO'
                reason  = f"⚡ LATE SUPPLEMENT — solar {solar_w}W, batt {soc:.0f}%"
                urgency = "medium"
 
            else:
                desired_output  = 'SUB'
                desired_charger = 'OSO'
                reason  = f"✅ NORMAL — batt {soc:.0f}%, balance {energy_balance:+.1f}kWh"
                urgency = "low"
 
        elif grid_info['state'] == 'absent':
            if battery_critical and solar_weak:
                desired_output  = 'SBU'
                desired_charger = 'CSO'
                reason  = f"🔴 SURVIVAL — batt {soc:.0f}%, weak sun!"
                urgency = "critical"
 
            elif battery_low and solar_weak:
                desired_output  = 'SBU'
                desired_charger = 'CSO'
                reason  = f"🟠 CONSERVE — batt {soc:.0f}%, weak solar"
                urgency = "medium"
 
            elif is_night:
                hours_to_sunrise = self._hours_until_sunrise()
                survival = self._survival_hours(soc)
                if survival < hours_to_sunrise:
                    desired_output  = 'SBU'
                    desired_charger = 'CSO'
                    reason  = (f"🔴 NIGHT DEFICIT — dies "
                               f"{hours_to_sunrise - survival:.1f}h before sunrise")
                    urgency = "high"
                else:
                    desired_output  = 'SBU'
                    desired_charger = 'CSO'
                    reason  = f"🌙 NIGHT OK — {survival:.1f}h battery left"
                    urgency = "low"
 
            else:
                desired_output  = 'SBU'
                desired_charger = 'CSO'
                reason  = f"🟢 OFF-GRID — solar {solar_w}W, batt {soc:.0f}%"
                urgency = "low"
 
        else:
            desired_output  = 'SBU'
            desired_charger = 'CSO'
            reason  = f"❓ Unknown state '{grid_info['state']}' — safe defaults"
            urgency = "medium"
 
        # ── FIX 5: decision stickiness ─────────────────────────────────────
        desired_output, desired_charger, was_held = self._apply_decision_stickiness(
            desired_output, desired_charger, urgency
        )
        if was_held:
            reason += " [held by stickiness]"
 
        # ── Apply settings ─────────────────────────────────────────────────
        out_changed, chr_changed = self._safe_write(desired_output, desired_charger)
 
        if out_changed or chr_changed:
            logger.info(
                f"✅ Settings → Output={desired_output}, Charger={desired_charger}"
            )
        else:
            logger.info(f"Settings unchanged — {reason}")
 
        # ── Log ────────────────────────────────────────────────────────────
        self.db.log_decision({
            'timestamp':       now.isoformat(),
            'grid_state':      grid_info['state'],
            'grid_stable':     grid_info['present'],
            'battery_soc':     soc,
            'solar_w':         solar_w,
            'forecast_kwh':    forecast_kwh,
            'output_priority': desired_output,
            'charger_priority':desired_charger,
            'output_changed':  out_changed,
            'charger_changed': chr_changed,
            'reason':          reason,
            'energy_balance':  energy_balance,
        })
 
        self.successful_polls += 1
 
        if urgency in ('critical', 'high'):
            self._alert(reason, desired_output, desired_charger, urgency)
 
        return desired_output, desired_charger, reason


    def _get_live_load_watts(self, solar_w: int) -> float:
        """Read output (load) power directly from inverter register 9+10."""
        try:
            raw = self.modbus.read_32bit_unsigned(9)   # REG output active power
            return int(raw / 10.0)
        except Exception as e:
            logger.error(f"Load read failed: {e}")
            return self.config.HOME_USAGE_WATTS


    def _print_summary(self):
        """Print system status summary"""
        logger.info("=" * 60)
        logger.info(f"Output: {self.current_output} | Charger: {self.current_charger}")
        logger.info(f"Writes today: {self.throttle.write_count_today}/{self.config.MAX_WRITES_PER_DAY}")
        if self.throttle.write_count_today > 0:
            years = 100_000 / (self.throttle.write_count_today * 365)
            logger.info(f"EEPROM lifespan: ~{years:.0f} years")
        logger.info(f"Polls: {self.successful_polls} | Dry run: {self.config.DRY_RUN}")
        if self.config.DRY_RUN:
            elapsed = (datetime.now() - self.dry_run_start).total_seconds() / 3600
            logger.info(f"Dry run: {elapsed:.1f}/{self.config.DRY_RUN_AUTO_DISABLE_HOURS}h")
        logger.info("=" * 60)

    def run(self):
        """Main loop"""
        logger.info("=" * 60)
        logger.info("🌍 GROWATT LEBANON CONTROLLER v6.0")
        logger.info("=" * 60)
        logger.info(f"📍 Beirut - Grid: ~4h/day | Batt: {self.config.BATTERY_CAPACITY}kWh")
        logger.info(f"🔁 Hysteresis: Critical {self.config.BATTERY_CRITICAL_THRESHOLD}→{self.config.BATTERY_CRITICAL_RECOVERY}%")
        logger.info(f"⚡ Grid: {self.config.GRID_STABILITY_MIN_VOLTAGE}-{self.config.GRID_STABILITY_MAX_VOLTAGE}V")
        logger.info(f"📊 Horizon: {self.config.HORIZON_HOURS}h | Rarity threshold: {self.config.GRID_RARITY_THRESHOLD_HOURS}h")
        logger.info(f"💾 EEPROM: {self.config.MAX_WRITES_PER_DAY} writes/day")
        logger.info(f"🧪 Dry run: {self.config.DRY_RUN}")
        logger.info("=" * 60)

        self._print_summary()
        self.make_decision()

        while self.running:
            try:
                time.sleep(self.config.POLL_INTERVAL_SEC)
                if not self.running:
                    break
                self.make_decision()
                if datetime.now().hour == 0 and datetime.now().minute < 6:
                    self._print_summary()
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(60)

        self._print_summary()
        logger.info("👋 Shutdown complete")







if __name__ == "__main__":
    controller = GrowattController()
    controller.run()
