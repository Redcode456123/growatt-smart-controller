**🙏 Acknowledgments**
**By using this software, you acknowledge that:**
- ❌ This code has NOT been tested on any real Growatt inverter
- ❌ The Modbus registers have NOT been verified on physical hardware
- ❌ The logic has NOT been validated in real-world conditions
- ❌ There may be bugs, errors, or incorrect register mappings

**What this means for you:**
- ⚠️ DO NOT run this on your live inverter without extensive testing
- ⚠️ ALWAYS use `DRY_RUN = True` mode first
- ⚠️ BACKUP your original inverter settings before any testing
- ⚠️ You accept full responsibility for any outcomes

---

## 📋 Project Status

| Aspect | Status |
|--------|--------|
| Code written | ✅ Complete |
| Logic designed | ✅ Complete |
| Register mapping (based on V0.14 spec) | ✅ Documented |
| Hardware testing | ❌ NOT DONE |
| Dry-run testing | ❌ NOT DONE |
| Production validation | ❌ NOT DONE |

**This project is seeking:**
- 🔍 Code review from experienced developers
- 🧪 Testers with real Growatt hardware (dry-run only)
- 📝 Feedback on logic and approach
- 🛠️ Help identifying register mapping issues

---

## 🎯 What This Script Is Designed To Do

This controller is designed to handle Lebanon's unique power situation:

**The Problem:**
- Grid available only 2-4 random hours per day
- No predictable schedule (completely random timing)
- Voltage fluctuates wildly (190V-260V)
- No net metering (can't sell excess solar)

**The Solution (Theoretical):**
- Detect grid immediately via Modbus
- Use weather forecast to avoid wasting grid on sunny days
- Protect battery with hysteresis (20%→30%, 40%→50%)
- Detect load spikes (AC startup) to protect battery
- Learn grid patterns over time (rarity tracking)

---

## 🧠 Decision Logic (Theoretical)

The script makes decisions based on:

| Priority | Condition | Action |
|----------|-----------|--------|
| 1 | Battery < 20% | Emergency - use everything to charge |
| 2 | Load spike detected | Switch to grid, protect battery |
| 3 | Pre-sunset (<2h, battery<70%) | Force charging for night |
| 4 | Severe deficit (< -2kWh) | Charge aggressively |
| 5 | Opportunity (rare grid + small deficit) | Opportunistic charge |
| 6 | Low battery + bad forecast | Capture grid window |
| 7 | Night + low battery | Charge from night grid |
| 8 | Normal operation | Save grid, use solar |

---

## 🔧 Hardware Required (If You Want To Test)

- Growatt SPF 6000ES Plus inverter
- Raspberry Pi 4 (2GB+ recommended)
- USB to RS485 adapter (isolated recommended)
- USB cable for connection

---

## 📦 Installation (For Testing)

```bash
# Clone the repository
git clone https://github.com/Redcode456123/growatt-lebanon-controller
cd growatt-lebanon-controller

# Install dependencies
pip install -r requirements.txt

# Create configuration
cp config.example.py config.py

# Edit config.py with your system values
nano config.py


## 🤖 Development Note

This project was developed with the assistance of **Claude (Anthropic)** , an AI language model.

**What AI helped with:**
- Code structure and architecture
- Modbus protocol implementation
- Documentation and README

**What AI cannot do:**
- Test on real hardware
- Verify register mappings
- Guarantee correct operation

**This disclosure is for transparency. The code requires human validation.**
