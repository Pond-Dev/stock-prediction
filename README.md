# TGXM WebTrader Demo Bot

บอทอ่านสัญญาณเข้า/ออกจากห้อง Telegram ที่อนุญาตไว้ แล้วประเมินความเสี่ยงก่อนกรอกและคลิกคำสั่งบน **XM WebTrader Demo** ผ่าน browser profile เฉพาะของบอท หน้าแดชบอร์ดภาษาไทยใช้ดูสถานะรวมบนเครื่องและไม่มีปุ่มส่งออเดอร์

> โครงการนี้รองรับบัญชี Demo เท่านั้น ไม่มีโหมด Live และไม่มีคำสั่งเปิด Live การทดสอบอัตโนมัติไม่ส่งออเดอร์ไปยัง XM

## ลำดับการทำงาน

```text
Telegram allowlist
  -> parser ตาม profile + ตรวจอายุ/Entry/SL/TP
  -> ตรวจ risk และ exposure
  -> บันทึก intent ลง SQLite ก่อนส่ง
  -> MT5 ตรวจ Demo account / symbol / tick / order_check แบบ read-only
  -> WebTrader อ่านบัญชีและฟอร์มกลับให้ตรงทุกช่อง
  -> คลิกปุ่ม Buy/Sell เพียงครั้งเดียว
  -> รับ ticket จากหน้าเว็บ
  -> MT5 ยืนยัน position + SL/TP ด้วย ticket เดียวกัน
  -> OPEN หรือ RECONCILE_REQUIRED (ห้ามลองส่งซ้ำ)
```

WebTrader เป็นช่องทางเดียวที่สร้างออเดอร์เมื่อใช้ `broker.adapter=xm_webtrader` ส่วน MT5 Python ใช้เป็นแหล่งหลักฐานก่อนและหลังคลิก ไม่ใช้ `order_send` ในเส้นทางนี้

## สิ่งที่ต้องมี

- Windows และ Python 3.11 หรือ 3.12 แบบ 64-bit
- XM MetaTrader 5 ที่ล็อกอินบัญชี Demo เดียวกับ WebTrader
- บัญชีแบบ `RETAIL_HEDGING`
- Telegram API ID/API hash จาก `my.telegram.org`
- เลข Demo account, ชื่อ server และ numeric Telegram peer ID ที่ต้องการอนุญาต

## ติดตั้ง

เปิด PowerShell ในโฟลเดอร์นี้:

```powershell
.\scripts\setup.ps1
Copy-Item config\settings.example.json config\settings.local.json
Copy-Item .env.example .env
```

สคริปต์จะสร้าง `.venv`, ติดตั้ง Telethon, MetaTrader5, Playwright, Chromium และชุดทดสอบ ไฟล์ `.env`, config ส่วนตัว, Telegram session, browser profile และฐานข้อมูลถูก `.gitignore` ไว้แล้ว

## ตั้งค่าครั้งแรก

1. ใส่ค่าต่อไปนี้ใน `.env` โดยไม่ใส่รหัสผ่าน XM:

   ```dotenv
   TGXM_TELEGRAM_API_ID=...
   TGXM_TELEGRAM_API_HASH=...
   TGXM_TELEGRAM_SESSION=state/telegram/tgxm
   TGXM_ALLOWED_DEMO_ACCOUNTS=เลขบัญชีเดโม
   TGXM_ALLOWED_DEMO_SERVERS=ชื่อเซิร์ฟเวอร์แบบตรงตัว
   ```

2. เปิดเมนูตั้งค่า:

   ```powershell
   .\scripts\tgxm.ps1 --config config\settings.local.json menu
   ```

   ตั้ง `broker.terminal_path`, คง `broker.adapter` เป็น `xm_webtrader`, และใส่ `peer_id` ให้ channel profile ที่จะอ่าน เริ่มด้วย `runtime.mode=observe` และ `trade_enabled=false`

3. สร้าง Telegram session และดู peer ID:

   ```powershell
   .\scripts\tgxm.ps1 --config config\settings.local.json telegram-login
   .\scripts\tgxm.ps1 --config config\settings.local.json telegram-dialogs
   ```

4. เปิด browser profile สำหรับ WebTrader:

   ```powershell
   .\scripts\tgxm.ps1 --config config\settings.local.json webtrader-login
   ```

   ล็อกอิน XM Demo ด้วยตัวเองในหน้าต่างที่เปิดขึ้น แล้วกด Enter ที่ PowerShell บอทจะเปรียบเทียบ Demo login/server บนหน้าเว็บกับหลักฐานจาก MT5 แบบตรงตัว รหัสผ่านอยู่ภายใต้ browser profile และไม่ผ่าน CLI/config ของบอท

5. ตรวจความพร้อม:

   ```powershell
   .\scripts\tgxm.ps1 --config config\settings.local.json doctor
   ```

## ใช้งาน

ทดสอบอ่าน Telegram โดยไม่เชื่อมส่งออเดอร์:

```powershell
.\scripts\tgxm.ps1 --config config\settings.local.json run
```

เปิดแดชบอร์ดสถานะภาษาไทยใน PowerShell อีกหน้าต่าง:

```powershell
.\scripts\tgxm.ps1 --config config\settings.local.json dashboard
```

จากนั้นเปิด `http://127.0.0.1:8765/` หน้าเว็บนี้ bind เฉพาะ loopback, อ่านอย่างเดียว และไม่แสดงข้อความ Telegram, เลขบัญชี, server หรือ secret

โหมดการทำงานมีสามระดับ:

- `observe` — อ่านและ parse เท่านั้น ไม่สร้าง broker
- `shadow` — ตรวจบัญชี/ตลาด/risk แต่ไม่เปิด browser และไม่คลิก
- `demo_armed` — เตรียมพร้อม แต่ยังไม่คลิกจนกว่าจะให้สิทธิ์ชั่วคราวตอนเริ่มโปรเซส

เมื่อ Observe และ Shadow ผ่านแล้ว ให้เปิด `trade_enabled=true` เฉพาะ channel ที่ตรวจแล้ว เปลี่ยน `runtime.mode=demo_armed` และสั่ง:

```powershell
.\scripts\tgxm.ps1 --config config\settings.local.json run --activate-demo
```

`--activate-demo` ไม่ถูกบันทึกลง config ต้องระบุใหม่ทุกครั้งที่เริ่มโปรเซส และยังต้องผ่าน Demo allowlist, Hedging, fresh tick, exposure cap, SL/TP, form read-back, price drift และ receipt reconciliation ทุกข้อ

## Indicator ทำนายราคา (แสดงผลอย่างเดียว)

```powershell
.\scripts\tgxm.ps1 --config config\settings.local.json predict
```

คำสั่งนี้ดึงราคาย้อนหลังจาก MT5 (ต้องตั้งค่า `broker.terminal_path` ไว้ก่อน) แล้วประเมินกฎ
EMA crossover + RSI + ATR แบบ deterministic เพื่อพิมพ์ผล Buy/Sell พร้อม SL/TP ที่คำนวณจาก ATR
ออกมาเป็น JSON **เป็นเครื่องมือให้ดูเฉย ๆ เท่านั้น** ไม่บันทึกลงฐานข้อมูล ไม่สร้าง Order Intent และ
ไม่เชื่อมกับเส้นทางส่งออเดอร์ Telegram → XM เลย ผลลัพธ์ `NO_SIGNAL` ถือเป็นผลปกติ ไม่ใช่ข้อผิดพลาด
ปรับพารามิเตอร์ (symbol, timeframe, ช่วง EMA/RSI/ATR) ได้ในเมนูตั้งค่าที่หัวข้อ `indicator`

## บอทเทรดอัตโนมัติจากอินดิเคเตอร์ (`tgxm autotrade`)

คำสั่งนี้เป็นเส้นทางที่ **สอง** ของโปรเจกต์: ไม่อ่าน Telegram เลย แต่ตัดสินใจเองจากกราฟ MT5
ด้วยกฎชุดเดียวกับ `pine/ema_rsi_atr_advisory.pine` แล้วส่งออเดอร์เข้าเทอร์มินัล MT5 เดโมที่เปิดอยู่

กฎเข้าไม้ (ต้องครบทุกข้อบนแท่งที่ **ปิดแล้ว** เท่านั้น):

1. EMA เร็วตัด EMA ช้า (ขึ้น = BUY, ลง = SELL)
2. RSI อยู่ในโซนหนุนทิศทางนั้น (50 < RSI < 70 สำหรับ BUY, 30 < RSI < 50 สำหรับ SELL)
3. ไทม์เฟรมใหญ่ต้องไปทางเดียวกัน (ค่าเริ่มต้นใช้บันไดแบบ Pine: M1 → M15, M15 → H1)

SL/TP คำนวณจาก ATR เหมือน Pine: `SL = ราคาปิด ∓ ATR×1.5`, TP ที่ส่งให้โบรกเกอร์คือเป้าที่ 2
(`ATR×3.0`) ส่วนเป้าที่ 1 (`ATR×1.5`) ใช้เป็นจุดเลื่อน SL มาเท่าทุน ทั้งสองค่าปรับได้ในเมนูตั้งค่า

กฎจัดการไม้ที่เปิดอยู่ (ตาม Pine เช่นกัน):

- ราคาแตะเป้าที่ 1 → เลื่อน SL มาที่ราคาเปิด (เท่าทุน)
- EMA ตัดกลับสวนทาง → ปิดไม้ทันที ไม่รอ SL

### เริ่มใช้งาน

```powershell
Copy-Item config\settings.example.json config\settings.local.json
Copy-Item .env.example .env
```

ใส่เลขบัญชีเดโมและชื่อเซิร์ฟเวอร์ใน `.env` (`TGXM_ALLOWED_DEMO_ACCOUNTS`, `TGXM_ALLOWED_DEMO_SERVERS`)
แล้วตั้งค่าในเมนู:

```powershell
.\scripts\tgxm.ps1 --config config\settings.local.json menu
```

ต้องตั้งอย่างน้อย:

- `broker.adapter` = `mt5` และ `broker.terminal_path` ชี้ไปที่ `terminal64.exe`
- `autotrade.enabled` = `true`, `autotrade.broker_symbol` = ชื่อ symbol ตรงตัวใน MT5 (เช่น `XAUUSD`)
- `autotrade.timeframe` = ไทม์เฟรมที่จะเทรด (เช่น `M1`)

**ต้องเปิดปุ่ม `Algo Trading` ในหน้าต่าง MT5 ด้วย** ไม่งั้นบอทจะหยุดพร้อมข้อความ
`external trading is not enabled` (MT5 ปฏิเสธ `order_send` ทุกครั้งเมื่อปุ่มนี้ปิด)

ลองเดินหนึ่งรอบแบบไม่ส่งออเดอร์:

```powershell
.\scripts\tgxm.ps1 --config config\settings.local.json autotrade --once
```

เปิดใช้จริงบนเดโม (ต้องตั้ง `autotrade.trade_enabled=true` ก่อน):

```powershell
.\scripts\tgxm.ps1 --config config\settings.local.json autotrade --activate-demo
```

`--activate-demo` เป็นสิทธิ์ชั่วคราวเหมือนคำสั่ง `run` ไม่ถูกบันทึกลง config ต้องใส่ใหม่ทุกครั้ง

### ด่านความปลอดภัยที่ยังบังคับอยู่

- บัญชีต้องเป็น Demo และตรงกับ allowlist ทั้งเลขบัญชีและชื่อเซิร์ฟเวอร์
- ปริมาณคงที่ `0.01 lot` ภายใต้ `risk.hard_lot_cap`
- หนึ่งไม้ต่อ symbol (`autotrade.max_open_positions`), เว้นระยะ `cooldown_bars` แท่ง,
  จำกัดจำนวนไม้ต่อวัน และบล็อกเมื่อสเปรดกว้างเกิน `max_spread_points`
- ถ้ามีโพซิชันอื่นบน symbol เดียวกันที่ไม่ใช่ของบอท จะไม่เข้าไม้ใหม่
- บันทึก Order Intent ลง SQLite ก่อนส่งเสมอ โดยผูกกับ "แท่งเทียนนั้นแท่งเดียว" ปิดโปรแกรมแล้วเปิดใหม่
  ก็เข้าซ้ำแท่งเดิมไม่ได้
- ผลลัพธ์กำกวมจะกลายเป็น `RECONCILE_REQUIRED` และไม่ส่งซ้ำเด็ดขาด
- การเลื่อน SL หรือปิดไม้ ต้องพิสูจน์ความเป็นเจ้าของ (magic + comment + intent) ก่อนทุกครั้ง

### เวลาเซิร์ฟเวอร์

MT5 ส่งเวลาของ **เซิร์ฟเวอร์โบรกเกอร์** ไม่ใช่ UTC (เซิร์ฟเวอร์เดโมส่วนใหญ่เป็น UTC+2/+3)
บอทวัดส่วนต่างนี้เองจากราคาล่าสุดตอนเริ่มทำงาน แล้วใช้ค่านั้นแปลงเวลาแท่งเทียนและ tick
ถ้าวัดไม่ได้ (ตลาดปิด/ราคาไม่ไหล) จะไม่เริ่มทำงาน ตั้งค่าตายตัวได้ที่
`broker.server_utc_offset_minutes`

## รันในตัว MT5 เอง (Expert Advisor)

ถ้าไม่อยากเปิดโปรเซส Python ค้างไว้ ใช้ [`mql5/TgxmEmaRsiAtrDemo.mq5`](mql5/TgxmEmaRsiAtrDemo.mq5)
แทนได้ เป็น EA ที่ใช้กฎเดียวกันทุกข้อ แต่รันอยู่ในเทอร์มินัล MT5 เอง ลากใส่กราฟแล้วจบ

```powershell
$data = "$env:APPDATA\MetaQuotes\Terminal\<terminal-id>\MQL5\Experts"
Copy-Item mql5\TgxmEmaRsiAtrDemo.mq5 $data
& "C:\Program Files\MetaTrader 5\MetaEditor64.exe" /compile:"$data\TgxmEmaRsiAtrDemo.mq5" /log
```

จากนั้นรีเฟรช Navigator ในเทอร์มินัล แล้วลาก `TgxmEmaRsiAtrDemo` ใส่กราฟ XAUUSD M1
รายละเอียดพารามิเตอร์ ข้อจำกัด และระดับการตรวจสอบอยู่ใน [mql5/README.md](mql5/README.md)

ต่างจากฝั่ง Python ตรงที่ EA ไม่มีฐานข้อมูล Order Intent ของตัวเอง ใช้ประวัติดีลของ MT5
เป็นแหล่งความจริงแทน และ **ไม่มีเทสต์อัตโนมัติครอบ** (ชุด `pytest` ครอบเฉพาะฝั่ง Python)

## ข้อจำกัดที่ตั้งใจไว้

- ถ้าผลหลังคลิกไม่ชัดเจน ระบบเปลี่ยนเป็น `RECONCILE_REQUIRED` และไม่คลิกซ้ำอัตโนมัติ
- receipt ต้องมี broker ID ที่จับคู่กับ position และ SL/TP ใน MT5 ได้แบบ exact จึงถือว่า `OPEN`
- สัญญาณ Entry Zone ที่ไม่สามารถรับประกัน fill ภายในช่วงด้วย Market Execution จะถูกบล็อกหรือรอ ไม่ลดกฎเพื่อไล่ราคา
- คำสั่งแก้ SL/TP, ปิดบางส่วน หรือปิดออเดอร์จากข้อความภายหลังเป็น `notify_only`
- หาก XM เปลี่ยน DOM/ป้ายชื่อ หน้าเว็บจะ fail closed ต้องให้ `webtrader-login` ตรวจ selector contract ใหม่ก่อนใช้งาน
- การใช้บัญชี Live ถูกปฏิเสธทั้ง config, MT5 identity และ WebTrader identity

## ตรวจสอบโครงการ

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m tgxm --help
```

คำสั่งเพิ่มเติมที่มีประโยชน์:

```powershell
.\scripts\tgxm.ps1 --config config\settings.local.json validate-config
.\scripts\tgxm.ps1 --config config\settings.local.json db-status
.\scripts\tgxm.ps1 --config config\settings.local.json show-config
```
