#!/usr/bin/env python3
"""
Configuration for Growatt Lebanon Solar Controller

INSTRUCTIONS:
1. Copy this file to 'config.py'
2. Edit the values marked with "CHANGE THIS" to match your system
3. Keep DRY_RUN = True for first 48 hours of testing

⚠️ WARNING: Never share your actual config.py online!
   This example file is safe to share.
"""

class Config:
    # ==========================================================================
    # SYSTEM LOCATION - CHANGE THESE
    # ==========================================================================
    LATITUDE = 33.89      # Your latitude (approx is fine for weather)
    LONGITUDE = 35.50     # Your longitude (approx is fine for weather)
    TIMEZONE = "Asia/Beirut"

    # ==========================================================================
    # SOLAR & BATTERY - CHANGE THESE
    # ==========================================================================
    PANEL_PEAK_REAL = 3000      # Your solar panel peak power in Watts
    INVERTER_EFF = 0.85         # Conservative efficiency (includes dust/losses)
    BATTERY_CAPACITY = 10.0     # Your battery capacity in kWh
    AVG_DAILY_USAGE = 5.6       # Your average daily consumption in kWh
    HOME_USAGE_WATTS = 233      # Average watts (calculated from daily usage)

    # ==========================================================================
    # HORIZON PLANNING (How far ahead to plan)
    # ==========================================================================
    HORIZON_HOURS = 12                  # Look ahead 12 hours
    DEFICIT_SEVERE_THRESHOLD = -2.0     # kWh - severe deficit (must charge)
    DEFICIT_OPPORTUNITY_THRESHOLD = -1.0  # kWh - opportunistic charge

    # ==========================================================================
    # GRID RARITY TRACKING (Learns your grid pattern)
    # ==========================================================================
    GRID_RARITY_THRESHOLD_HOURS = 6     # Grid is "rare" if every 6+ hours
    GRID_RARITY_SAMPLE_SIZE = 10        # Track last 10 grid appearances

    # ==========================================================================
    # GRID STABILITY (Protects from Lebanon's voltage fluctuations)
    # ==========================================================================
    GRID_STABILITY_DELAY_SEC = 120           # Wait 2 min before trusting grid
    GRID_STABILITY_MIN_VOLTAGE = 190         # Volts - reject below (brownout)
    GRID_STABILITY_MAX_VOLTAGE = 260         # Volts - reject above (over-voltage)
    GRID_STABILITY_RECOVER_VOLTAGE = 255     # Volts - re-accept below this
    GRID_STABILITY_MIN_FREQ = 49.0           # Hz
    GRID_STABILITY_MAX_FREQ = 51.0           # Hz

    # ==========================================================================
    # BATTERY PROTECTION (Hysteresis prevents toggling)
    # ==========================================================================
    BATTERY_CRITICAL_THRESHOLD = 20     # % - enter critical mode below this
    BATTERY_CRITICAL_RECOVERY = 30      # % - leave critical mode above this
    BATTERY_LOW_THRESHOLD = 40          # % - enter low mode below this
    BATTERY_LOW_RECOVERY = 50           # % - leave low mode above this

    # ==========================================================================
    # SOLAR THRESHOLDS
    # ==========================================================================
    SOLAR_STRONG_THRESHOLD = 1500       # Watts
    SOLAR_WEAK_THRESHOLD = 500          # Watts

    # ==========================================================================
    # WEATHER FORECAST
    # ==========================================================================
    DEFAULT_FORECAST_KWH = 3.5          # Conservative default for Lebanon winter
    FORECAST_CACHE_HOURS = 6            # Cache forecast for 6 hours
    FORECAST_DECAY_START_HOURS = 24     # Start decaying after 24 hours
    FORECAST_DECAY_MAX_HOURS = 48       # Decay to 50% after 48 hours

    # ==========================================================================
    # SPIKE DETECTION (Protects battery from AC/water heater startup)
    # ==========================================================================
    SPIKE_FAST_INTERVAL = 2             # seconds - how often to check
    SPIKE_EMA_ALPHA = 0.2               # smoothing factor for load average
    SPIKE_DELTA_W = 700                 # Watts - jump above baseline
    SPIKE_ABSOLUTE_W = 1800             # Watts - absolute heavy load
    SPIKE_HOLD_SECONDS = 180            # seconds - keep spike mode active

    # ==========================================================================
    # MODBUS COMMUNICATION (Usually fine with defaults)
    # ==========================================================================
    MODBUS_PORT = '/dev/ttyXRUSB*'      # Auto-detects (or set specific like '/dev/ttyUSB0')
    MODBUS_SLAVE_ID = 1
    MODBUS_BAUDRATE = 9600
    MODBUS_TIMEOUT = 2
    MODBUS_RETRY_COUNT = 3
    MODBUS_RETRY_DELAY = 5

    # ==========================================================================
    # MODBUS REGISTER ADDRESSES (Verified for SPF 6000ES Plus V0.14)
    # ==========================================================================
    # Input Registers (read with function code 04)
    REG_INPUT_SYSTEM_STATUS = 0
    REG_INPUT_PV1_VOLT = 1
    REG_INPUT_PV2_VOLT = 2
    REG_INPUT_PV1_PWR_H = 3
    REG_INPUT_PV2_PWR_H = 5
    REG_INPUT_BAT_VOLTAGE = 17
    REG_INPUT_BAT_SOC = 18
    REG_INPUT_GRID_VOLTAGE = 20
    REG_INPUT_GRID_FREQ = 21
    REG_INPUT_AC_POWER_H = 36
    REG_INPUT_FAULT_CODE = 40
    REG_INPUT_BAT_POWER = 77
    REG_INPUT_BMS_SOC = 203
    REG_INPUT_BMS_VOLT = 204

    # Holding Registers (read/write with function codes 03/06) - EEPROM!
    REG_HOLD_STANDBY = 0
    REG_HOLD_OUTPUT_PRIORITY = 1        # 0=UTI, 1=SBU, 2=SUB
    REG_HOLD_CHARGER_PRIORITY = 2       # 0=CSO, 1=CUE, 2=OSO (FIXED: was 14!)

    # Output priority encoding (for Setting 01)
    OUTPUT_UTI = 0   # Utility First
    OUTPUT_SBU = 1   # Solar → Battery → Utility
    OUTPUT_SUB = 2   # Solar → Utility → Battery

    # Charger priority encoding (for Setting 14)
    CHARGER_CSO = 0  # PV Only - solar only charging
    CHARGER_CUE = 1  # PV + Utility - both simultaneously
    CHARGER_OSO = 2  # PV Priority - solar first, grid backup

    # ==========================================================================
    # POLLING & SYNC
    # ==========================================================================
    POLL_INTERVAL_SEC = 300              # 5 minutes between decisions
    FORCE_SYNC_INTERVAL_HOURS = 24       # Re-read settings every 24 hours

    # ==========================================================================
    # EEPROM PROTECTION
    # ==========================================================================
    WRITE_THROTTLE_SEC = 60              # Minimum seconds between writes
    MAX_WRITES_PER_DAY = 50              # Maximum writes per day

    # ==========================================================================
    # DATABASE (SD card optimized)
    # ==========================================================================
    DB_PATH = '/home/pi/growatt_history.db'
    DB_RETENTION_DAYS = 30
    DB_JOURNAL_MODE = 'WAL'

    # ==========================================================================
    # SAFETY - CRITICAL: KEEP TRUE FOR FIRST 48 HOURS OF TESTING
    # ==========================================================================
    DRY_RUN = True                       # True = no writes to inverter
    DRY_RUN_AUTO_DISABLE_HOURS = 48
    
    # Lebanon has no net metering - prevents attempted grid export
    NO_EXPORT = True


# ==============================================================================
# EXAMPLE: How to use this config
# ==============================================================================
if __name__ == "__main__":
    # Test the configuration
    cfg = Config()
    print("=" * 50)
    print("Growatt Lebanon Controller - Configuration Example")
    print("=" * 50)
    print(f"Location: {cfg.LATITUDE}, {cfg.LONGITUDE}")
    print(f"Battery: {cfg.BATTERY_CAPACITY} kWh")
    print(f"Solar Peak: {cfg.PANEL_PEAK_REAL} W")
    print(f"Daily Usage: {cfg.AVG_DAILY_USAGE} kWh")
    print(f"DRY_RUN Mode: {cfg.DRY_RUN}")
    print("=" * 50)
    print("\n✅ Configuration loaded successfully!")
    print("📝 Copy this file to 'config.py' and edit values for your system")
