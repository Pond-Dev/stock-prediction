# XAUUSD Short Scalper (TradingView, 1m / 3m / 5m, SHORT only)

ไฟล์ในชุดนี้:

| ไฟล์ | หน้าที่ |
|---|---|
| `xauusd_short_scalper.pine` | **Indicator** สำหรับดู chart + signal + dashboard + alert (ไม่ส่งคำสั่ง) |
| `xauusd_short_scalper_strategy.pine` | **Strategy** สำหรับ backtest ใน Strategy Tester ใช้ core logic เดียวกัน |
| `../scripts/check_pine_core.py` | พิสูจน์ว่า core ของสองไฟล์ตรงกันแบบ byte-identical และไม่มี `request.security` / `lookahead` / `varip` |

สถานะปัจจุบัน (สำคัญ):

- **Implemented**: logic ทั้งหมดในเอกสารนี้อยู่ในสองไฟล์ Pine แล้ว
- **Tested**: เฉพาะ static check (`python scripts/check_pine_core.py`) เท่านั้น สภาพแวดล้อมนี้ **ไม่มี Pine compiler**
  ครั้งแรกที่วางลง Pine Editor ถ้ามี syntax error ให้ส่ง error กลับมาแก้
- **ยังไม่มีผล backtest จริง**: Backtest / OOS / Walk-forward / Robustness report ด้านล่างเป็น *template + วิธีรัน*
  ตัวเลขต้องมาจากการรันบน TradingView เท่านั้น ห้ามเอา template ไปอ้างเป็นผลลัพธ์

## 1. ติดตั้ง

1. เปิด chart **XAUUSD** timeframe 1m / 3m / 5m
2. Pine Editor → New → Blank indicator → วางเนื้อหา `xauusd_short_scalper.pine` → Add to chart
3. สำหรับ backtest ทำแบบเดียวกันกับ `xauusd_short_scalper_strategy.pine` (เลือก Blank strategy)
4. Dashboard จะเตือน `(not gold!)` สีส้มถ้า symbol ไม่ใช่ XAU/GOLD ค่า default ทั้งหมดจูนสำหรับทองเท่านั้น

## 2. สถาปัตยกรรม 5 ชั้น (Layer)

```
Layer 1  Short Bias        NO SHORT / WATCH / SHORT BIAS
Layer 2  Setup detection   A Pullback · B Breakdown · C Rejection · D Momentum continuation
Layer 3  Score 0-100       Trend 20 · Structure 20 · Momentum 20 · Confirmation 15 · Volatility 10 · Session 10 · Entry quality 5
Layer 4  Trade quality     session · volatility · anti-chasing · SL range · reward · room  → ไม่ผ่าน = NO TRADE
Layer 5  Lifecycle         WATCH → PRE-SHORT → SHORT → QUICK PROFIT → EXIT / STOPPED / INVALIDATED
```

### 2.1 Phase 1 – Market structure

- Swing high/low ใช้ `ta.pivothigh/low(swingLen, swingLen)` (default 3) **ยืนยันหลังปิดอีก 3 แท่ง** จึงเกิดช้าแต่ไม่เคยขยับ
- เก็บ swing สองตัวล่าสุด → `LH` (lower high), `LL`, `HH`, `HL`
- **Break of structure (BOS)** = แท่ง *ปิด* ทะลุ swing ที่ยืนยันแล้ว นับครั้งเดียวต่อ swing
- `structTrend`: -1 เมื่อ LH+LL หรือ BOS ลง, +1 เมื่อ HH+HL หรือ BOS ขึ้น, 0 ถ้าไม่ชัด
- Resistance = swing high ล่าสุด, Support = swing low ล่าสุด (fallback = highest/lowest 20 แท่งก่อนหน้า)
- บน chart: เส้น step สีแดง/teal (ปรากฏหลังยืนยันเท่านั้น)

### 2.1b Higher-timeframe bias (เพิ่มรอบสอง หลังพบว่า 1m ไม่มี edge)

- อ่าน EMA 21 / EMA 50 และ close ของ **แท่งที่ปิดแล้วล่าสุด** ของ timeframe ที่สูงกว่า ผ่าน
  `request.security(..., expr[1], lookahead = barmerge.lookahead_on)` ซึ่งเป็น idiom non-repaint ของ TradingView
  (historical และ realtime เห็นค่าเดียวกัน) `scripts/check_pine_core.py` บังคับว่าใช้ได้เฉพาะรูปแบบนี้
- Auto ladder: 1–3m → 15m, 5–15m → 1H, 30m–1H → 4H หรือเลือก Fixed
- HTF **bullish** (EMA21 > EMA50 และ close > EMA21) → บังคับ NO SHORT ทุก setup (แท่งเขียว, พื้นหลังแดง)
- HTF **bearish** → +1 คะแนน bias
- HTF ไม่สูงกว่า chart หรือยังโหลดไม่เสร็จ → บล็อกทุก short (fail closed) พร้อมเหตุผล `higher TF invalid / loading`
- ปิดได้ด้วย `Higher-timeframe bias filter` แต่ไม่แนะนำสำหรับ 1m

### 2.2 Phase 2 – Short bias (Layer 1)

คะแนน bias 0-8: EMA9<EMA21<EMA50 (+2, หรือ EMA9<EMA50 +1), close<EMA21 (+1), slope EMA21 ลง (+1),
structure bearish (+2), RSI<50 (+1), BOS ลงภายใน 20 แท่ง (+1)

| bias | เงื่อนไข |
|---|---|
| NO SHORT | < 3 หรือ มี BOS ขึ้นล่าสุด + structure bullish |
| WATCH | 3-4 |
| SHORT BIAS | ≥ 5 |

### 2.3 Setup A – Pullback short (Phase 3)

1. EMA9 < EMA50 และ structure ไม่ bullish, bias ≥ WATCH
2. ราคาดีดขึ้นแตะโซน EMA9 … EMA50+0.3 ATR ภายใน 3 แท่งล่าสุด (`pbZone`) → **PRE-SHORT (armed)**
3. Trigger: แท่งปิดกลับลงใต้ EMA9, เป็นแท่งแดง, RSI ลง, และมี confirmation อย่างใดอย่างหนึ่ง:
   ปิดต่ำกว่า low แท่งก่อน / ปิดในช่วง 30% ล่างของแท่ง / ไส้บนยาวกว่าตัว
4. Confirmation score: ปิดต่ำกว่า low ก่อน + ปิดแรง = 15, ปิดต่ำกว่า low ก่อน = 11, อื่น ๆ = 8
5. Structure SL = highest high ของ pullback window (8 แท่ง) + buffer

### 2.4 Setup B – Breakdown short (Phase 4)

1. มี swing low ที่ยืนยันแล้ว และยังไม่ถูกทะลุ
2. ราคาลงมาทดสอบ (low ≤ support + 0.3 ATR แต่ยังปิดเหนือ) → **armed**
3. Trigger = แท่ง *ปิด* ต่ำกว่า support อย่างน้อย `0.10 ATR` (**ไส้แทงไม่นับ**) พร้อมแท่งแรง (ปิดใน 35% ล่าง, range ≥ 0.7 ATR)
   ตั้ง `Closes below support required = 2` ถ้าต้องการรอแท่งที่สองยืนยัน (เข้าช้ากว่า, false breakdown น้อยกว่า)
4. Confirmation: ปิดใน 20% ล่าง + range ≥ 1 ATR = 15, ไม่งั้น 10 (โหมด 2 แท่ง: 15/12)
5. Structure SL = max(support ที่ทะลุ, highest high 3 แท่ง) + buffer
6. Extreme volatility อนุญาตเฉพาะ setup นี้ที่ confirmation = 15

### 2.5 Setup C – Rejection short (Phase 5)

1. Spike ขึ้นเร็ว ≥ 0.6 ATR (จาก open หรือจาก low 3 แท่ง)
2. แตะ resistance: swing high / EMA50 (เมื่ออยู่ใต้) / high 20 แท่ง (ภายใน 0.1-0.15 ATR) → **armed**
3. Rejection bar: ไส้บน ≥ 50% ของ range และปิดใน 40% ล่าง
4. Trigger A = rejection bar ปิดแดง + RSI ลง (15 ถ้าไส้ ≥ 60%, ไม่งั้น 10)
   Trigger B = แท่งถัดไปปิดต่ำกว่า low ของ rejection bar (12)
5. Structure SL = high ของ rejection bar + buffer
6. ต้องมี bias ≥ WATCH (ปิดได้ด้วย `Rejection needs at least WATCH bias`)

### 2.6 Setup D – Momentum continuation short

1. SHORT BIAS, slope EMA9 ≤ -0.25 ATR ต่อ 3 แท่ง, RSI < 45
2. Consolidation: 4 แท่งก่อนหน้า range รวม ≤ 1.2 ATR และ high ไม่เกิน EMA21+0.2 ATR → **armed**
3. Trigger = ปิดต่ำกว่า low ของ consolidation ด้วยแท่งแดง
4. **ห้าม** ถ้าราคาลงจาก high 20 แท่งเกิน 3 ATR แล้ว (`mcMaxDropAtr`)
5. Structure SL = high ของ consolidation + buffer

ถ้าหลาย setup trigger แท่งเดียวกัน เลือก confirmation สูงสุด (เสมอกัน: Breakdown > Momentum > Pullback > Rejection)

## 3. Score (Layer 3)

| ส่วน | เต็ม | วิธีคิด |
|---|---|---|
| Trend | 20 | EMA stack bearish 10 (EMA9<EMA50 อย่างเดียว 6) + EMA21 slope ลง 5 + close < EMA21 5 |
| Structure | 20 | structure bearish 10 (ไม่ชัด 4) + BOS ลงล่าสุด/LH 5 + location ดี 5 |
| Momentum | 20 | RSI<50 5 + RSI ลง 5 + ปิดใน 30% ล่าง 5 + close < close[3] 5 |
| Confirmation | 15 | ตาม setup (ข้อ 2) |
| Volatility | 10 | NORMAL 10 · HIGH 5 · LOW 3 · EXTREME 0 |
| Session | 10 | ค่าที่ตั้งต่อ session (default Asian 4, London 8, NY 10, Overlap 10, นอก session 0) |
| Entry quality | 5 | SL ≤ 1.0 ATR 3 (≤ 1.5 ATR 2) + support ห่างพอถึง TP 2 |

Threshold ปรับได้: `< WEAK(60)` NO TRADE · `60-69` WEAK (ไม่เทรด เว้นแต่เปิด `Trade WEAK scores`) · `70-79` GOOD SHORT · `≥ 80` HIGH QUALITY (STRONG SHORT)

Dashboard แสดงรายละเอียดเป็น `T.. S.. M.. C.. V.. Se.. Q..`

## 4. Filters (Layer 4) – ไม่ผ่านข้อใดข้อหนึ่ง = NO TRADE พร้อมเหตุผลในแถว `Filter`

**Anti-chasing** (Phase 7) – ห้าม short เมื่อ: ลงจาก high 5 แท่ง > 2.5 ATR · แท่งแดงใหญ่ > 2 ATR ·
อยู่ต่ำกว่า EMA21 > 2 ATR · ATR ขยาย > 2 เท่าเทียบ 5 แท่งก่อน

**Volatility** (Phase 6) – ATR / SMA(ATR,100): LOW < 0.7 (บล็อก เว้นแต่ `Allow LOW`) · NORMAL · HIGH > 1.5 (ต้อง score ≥ STRONG) ·
EXTREME > 2.2 (เฉพาะ Breakdown confirmation 15)

**Session** – เวลาอ่านตาม `Session timezone` (default `Etc/UTC`): Asian 00:00-08:00, London 07:00-16:00, NY 13:00-21:00,
Overlap = London ∩ NY (13:00-16:00) นอกช่วงทั้งหมด = OFF (default ไม่อนุญาต) ปิด/เปิดและให้คะแนนแต่ละ session ได้
Dashboard แสดง `Session` และ `Session short quality` (คะแนนที่ตั้ง; ใน Strategy ยังมี win% ที่วัดจริงต่อ session)

**Risk** – SL ต้องอยู่ใน `[0.4, 2.0] ATR` · reward ≥ `0.3 R` · support ที่ใกล้สุดต้องห่างอย่างน้อย 50% ของระยะ TP (ยกเว้น TP โหมด Micro structure)

## 5. Stop Loss / Take Profit / Quick TP / Break-even (Phase 8-9)

| | โหมด | ค่า default |
|---|---|---|
| SL | Structure (structure high + 0.1 ATR) / ATR (1.0 ATR) / **Hybrid** = structure ที่ถูก clamp ใน [0.4, 2.0] ATR | Hybrid |
| TP | **Fixed R** (1.0 R) / ATR (0.3 ATR) / Micro structure (support ใกล้สุด + 0.05 ATR, ไม่มี → ใช้ ATR) | Fixed R |
| Quick TP | `0.3 R` → สถานะ QUICK PROFIT + alert; ถ้า `Exit at close when a bullish bar prints after Quick TP` เปิด และแท่งถัดมาปิดเขียวแต่ยังกำไร → EXIT ที่ราคาปิด (QUICK EXIT) | on |
| Break-even | กำไรถึง 0.5 R → SL = entry | on |
| Lock | กำไรถึง 0.8 R → SL = entry − 0.2 R | on |
| Time stop | 20 แท่ง (0 = ปิด) | 20 |
| Invalidation | ปิดเหนือ swing high ล่าสุด → EXIT | on |
| Cooldown | 2 แท่งหลังปิด trade | 2 |

หมายเหตุ: TP 1.0 R กับ Lock 0.8 R ทำงานร่วมกันได้ ถ้าตั้ง TP ต่ำกว่า trigger ของ BE/Lock ตัวนั้นจะไม่มีวันทำงาน (ตั้งใจให้ผู้ใช้เลือกเอง)
SL เลื่อนได้ **ลงอย่างเดียว** และมีผลตั้งแต่แท่งถัดไป (แท่งที่แตะ trigger ไม่ได้ประโยชน์จากมันเอง)

## 6. Lifecycle และการอ่าน chart

| State | ความหมาย |
|---|---|
| NO SHORT | bias ไม่เหมาะ |
| WATCH | bias ≥ WATCH แต่ยังไม่มี setup armed |
| PRE-SHORT | setup armed (จุดเทาเล็กเหนือแท่ง) หรือ trigger แล้วแต่ score WEAK |
| SHORT / STRONG SHORT | trigger + ผ่านทุก filter → label `SHORT 82` พร้อม Entry/SL/TP/R:R + เส้น entry (เทา), SL (แดง), TP (เขียว), Quick TP (เขียวประ) |
| QUICK PROFIT | low แตะ Quick TP |
| EXIT | TP HIT / QUICK EXIT / TIME STOP |
| STOPPED | แตะ SL (STOPPED / BREAK-EVEN / LOCKED PROFIT) |
| INVALIDATED | ปิดเหนือ swing high ระหว่างถือ |

### โหมด Minimal (default: `Chart detail = Minimal`)

บน chart จะเหลือแค่: แท่งสีตามแนวโน้ม (แดง = ฝั่ง short, เขียว = ไม่ใช่), พื้นหลัง (เขียว = เล่นได้ รอ signal, แดง = อยู่เฉย ๆ),
ป้าย `SHORT 84` ที่แท่ง signal, **กรอบแดง = entry→SL (risk), กรอบเขียว = entry→TP (reward)** ยืดตามแท่งขณะถือ (กรอบแดงกลายเป็น teal เมื่อ SL ≤ entry),
เส้น SL แดง / TP เขียว พร้อมป้ายราคา, เส้น entry จุดเทา, ป้ายผลลัพธ์ตัวเลขเดียว
(`TP +2.72`, `SL −3.17`, `BE +0.00`, `LOCK +0.9`, `EXIT +1.7`) และ status 1 บรรทัดข้างแท่งล่าสุด:
`NO SHORT` → `WATCH` → `GET READY` → `SHORT NOW` → `IN TRADE +1.23 · QUICK ✓` → `WAIT` (cooldown) หรือ `NO TRADE · เหตุผล`
EMA, S/R, จุด PRE-SHORT, โซนสี, ป้าย Quick/Entry, ป้าย SL-move, ตาราง ถูกซ่อนหมด เปลี่ยน `Chart detail = Custom` เพื่อเปิดทีละอย่าง
**อย่าใส่ strategy ไว้บน chart พร้อม indicator** เพราะ strategy วาด label ของตัวเองซ้ำอีกชุด ใช้ strategy เฉพาะตอน backtest

### สีแท่งเทียน (`Candle colors`, default = By trend)

| สีแท่ง | สถานะ | ทำอะไร |
|---|---|---|
| เทา | NO SHORT | ไม่ต้องทำอะไร |
| ฟ้า | WATCH | bias เริ่มเหมาะ ยังไม่มี setup |
| เหลือง | PRE-SHORT | setup กำลัง arm รอแท่งปิดยืนยัน |
| ม่วงบานเย็น | SHORT signal bar | แท่งที่ให้เข้า (มี label SHORT กำกับ) |
| เขียวสด | ถืออยู่และกำไร (close ต่ำกว่า entry) | ถึง Quick TP จะถอนก็ได้ |
| แดง | ถืออยู่และขาดทุน (close สูงกว่า entry) | ดู SL |
| แท่งที่ปิด trade | เขียวสด = กำไร, แดง = ขาดทุน, เทา = เท่าทุน | ดู label ผลลัพธ์ |

โหมดนี้ทับสีแดง/เขียวปกติของแท่ง ถ้าอยากเห็นทิศทางแท่งแบบเดิมให้เลือก `Off`

### สีเส้นและ label

| สี | คือ |
|---|---|
| เส้นส้ม / แดง / เทา | EMA 9 (fast) / EMA 21 (mid = dynamic resistance) / EMA 50 (slow = bias) |
| ขั้นบันไดแดงจาง / teal จาง | resistance = swing high ล่าสุดที่ยืนยัน / support = swing low ล่าสุด |
| เส้นแดงหนา / เขียวหนา / เขียวประ / เทาจุด | SL / TP / Quick TP / Entry ของ trade ที่ติดตาม |
| label แดงสด / แดงเข้ม | STRONG SHORT (≥ 80) / SHORT (70-79) |
| label เขียว / teal / เทา / ส้ม | TP HIT, QUICK EXIT / LOCKED PROFIT, SL → BE / BREAK-EVEN, STOPPED / TIME STOP, INVALIDATED |

### อ่านจาก chart อย่างเดียว (ไม่ต้องเปิดตาราง)

| บน chart | ความหมาย |
|---|---|
| **Status bubble** ขวาของแท่งล่าสุด | บรรทัดแรก = ต้องทำอะไรตอนนี้: `NO SHORT` / `WATCH` / `PRE-SHORT · PULLBACK arming` / `SHORT NOW 76` / `NO TRADE 62 + เหตุผล` / `COOLDOWN` / ระหว่างถือ = `SHORT … P/L +1.23 (+0.26R)` และ SL/TP ปัจจุบัน สีเขียว = กำไร, แดงเข้ม = ขาดทุน บรรทัดสอง = session · volatility · bias |
| Label `SHORT 76 · REJECTION` | signal ที่แท่งปิด พร้อม Entry, SL (risk เป็นราคา), TP (เป็น R) |
| โซนแดง / เขียว | ระยะ entry→SL (risk) และ entry→TP (reward) ยืดไปเรื่อย ๆ ขณะถือ โซนแดงเปลี่ยนเป็น teal เมื่อ SL ≤ entry แล้ว (ไม่มี risk) |
| ป้ายราคาปลายเส้น | `SL 4475.52 (break-even)`, `TP 4465.96`, `Quick 4469.30 ✓`, `Entry 4470.74` ขยับตามแท่งล่าสุด |
| `QUICK TP ✓` ใต้แท่ง | แตะ Quick TP แล้ว จะถอนเลยหรือเลื่อน SL ไป entry ก็ได้ |
| `SL → break-even` / `SL → lock` | จุดที่ SL ถูกเลื่อน เส้น SL เก่าหยุดและเส้นใหม่เริ่มที่ระดับใหม่ |
| Label ผลลัพธ์ `TP HIT +4.78 (+1.0R)` | ปิด trade พร้อมกำไร/ขาดทุนเป็นราคาและ R (`STOPPED`, `BREAK-EVEN`, `LOCKED PROFIT`, `QUICK EXIT`, `TIME STOP`, `INVALIDATED`) |
| วงกลมเทา `PB` `BD` `RJ` `MC` | setup ไหนกำลัง arm (PRE-SHORT) |
| พื้นหลังแดงจาง / ส้มจาง | SHORT BIAS / WATCH (ปิดได้ใน Display) |
| `✕ 62 chasing: …` (ปิด default) | trigger ที่โดน filter บล็อกพร้อมเหตุผล เปิดได้ที่ `Mark triggers a filter blocked` |

ค่า input ทั้งหมดถูกซ่อนจากแถบสถานะ (`display = display.none`) และ EMA / S-R ไม่แสดงตัวเลขบนแถบสถานะและแกนราคา
ตาราง dashboard ยังอยู่แต่ **ปิดเป็น default** (`Show dashboard table`)
Indicator ติดตาม **ครั้งละ 1 trade** และเก็บประวัติไว้บน chart หลายสิบ trade ล่าสุด (ตาม `max_labels_count` / `max_lines_count` / `max_boxes_count`)

## 7. Alerts (Phase 10)

สองทางเลือก:

1. **alertcondition** (เลือก condition ตอนสร้าง alert): Short Setup (PRE-SHORT), Short Entry, Strong Short, Quick TP,
   Break-even / Lock, Exit, Stop Loss, Invalidated ข้อความมี `{{ticker}} {{interval}}` และตัวเลขจาก hidden plot:
   `{{plot("Score")}} {{plot("Entry")}} {{plot("SL")}} {{plot("TP")}} {{plot("Quick TP")}} {{plot("Setup code")}}`
   (Setup code: 1 PULLBACK, 2 BREAKDOWN, 3 REJECTION, 4 MOMENTUM — placeholder ส่ง string ไม่ได้)
2. **alert() stream** (condition = "Any alert() function call"): ข้อความเต็ม
   `XAUUSD 3m | STRONG SHORT | score 84 | entry … | SL … | TP … | quick … | setup PULLBACK`
   เพิ่ม PRE-SHORT เข้า stream ได้ด้วย `Include PRE-SHORT in the alert() stream`

Entry/PRE-SHORT alert เป็นจริงเฉพาะ tick ปิดแท่ง → ใช้ **Once Per Bar Close**
Quick TP / SL / TP เป็น touch ของ high/low → **Once Per Bar** ได้แจ้งเร็วสุด

## 8. กฎ non-repaint (Phase ทุกเฟส – MANDATORY)

สิ่งที่รับประกัน:

- ทุก signal ประเมินบน **แท่งปิด** (`barstate.isconfirmed`) — historical bar และ realtime bar ใช้ code path เดียวกัน
  cross ที่โผล่กลางแท่งแล้วหายก่อนปิดไม่สร้าง label, ไม่เปลี่ยน state, ไม่ยิง alert
- Swing / S-R เกิดหลังยืนยัน `swingLen` แท่ง และไม่เคยขยับ (เครื่องหมาย swing วาดย้อนหลัง `offset=-swingLen` แต่ปรากฏหลังยืนยันเท่ากันทั้ง historical/realtime)
- ไม่มี `request.security`, `lookahead`, `varip`, `barstate.isrealtime` (script ตรวจ)
- Session ใช้ `time()` ซึ่ง deterministic

ข้อจำกัดที่ต้องรู้ (ไม่ใช่ repaint แต่เป็น model):

- **Touch detection** ของ SL/TP/Quick TP ใช้ high/low ของแท่งขณะกำลังก่อตัว การแตะ "ย้อนกลับไม่ได้" แต่ **ลำดับ** ในแท่งเดียวกันไม่รู้ →
  Indicator ถือ SL ก่อนเสมอ (conservative) Strategy ใช้ broker emulator ของ TradingView ซึ่งสมมติ path ภายในแท่ง → เปิด **Bar Magnifier** ถ้าแพลนมี
- Indicator ใช้ **close ของแท่ง signal** เป็น reference entry (ผู้ใช้ตัดสินใจตอนนั้น) Strategy fill ที่ **open แท่งถัดไป** (execution delay)
  ระดับ SL/TP/Quick TP ผูกกับ reference เดียวกัน; break-even ใน Strategy ใช้ราคา fill จริง
- Dashboard แสดงค่า live ของแท่งที่กำลังก่อตัว (bias/vol/score) เฉพาะ *state และ trade* ที่เป็น closed-bar

## 9. Strategy: execution model (Phase 11)

| รายการ | ค่า | แก้ที่ |
|---|---|---|
| Fill | signal @ close → market @ next open, `process_orders_on_close=false`, `calc_on_every_tick=false` | code |
| Spread | `0.25` → เลื่อนระดับ exit ลงเท่า spread (short ต้อง cover ที่ ask) | Settings › Execution model |
| Spread cost | `commission 0.125 cash/contract` ×2 orders = 0.25 ต่อ round trip | Properties (แก้คู่กับ spread) |
| Slippage | 2 ticks ทุก market fill | Properties |
| Mid-price chart (Pyth) | เปิด `Chart price is MID` → เลื่อน exit แค่ spread/2 และเพิ่ม slippage อีก spread/2 (เป็น tick) | Settings + Properties |
| Size | 1 contract (ไม่ศึกษา position sizing) | Properties |
| Partial | `Partial exit at Quick TP` (50%) เป็น option | Settings |

ห้ามอ้าง backtest ที่ entry = ราคาปิดแท่ง signal หรือ exit = high/low พอดี — strategy นี้ไม่ทำแบบนั้น

### Metrics ที่ dashboard ของ Strategy คำนวณ (มุมขวาล่าง / ซ้ายล่าง)

Total positions (และ legs ถ้ามี partial), Win rate, Profit factor, Expectancy ($ และ R), **Median profit / trade** ($ และ R),
Net profit, Max drawdown, Avg win / loss, Avg R, Median duration (bars → นาที), Longest losing streak, Avg trades per session,
ตารางแยกตาม **Session** (5 แถว + trades ต่อ session instance) และตาม **Setup** (4 แถว)
Performance by timeframe = รัน 1m / 3m / 5m แยกกันแล้วกรอกตาราง

## 10. Backtest protocol (Phase 11-13) – ต้องทำตามลำดับ ห้ามข้าม

### 10.1 Out-of-sample split (Phase 12)

`Backtest window` มี 4 โหมด: Train (60% แรกของแท่งที่โหลด), Validation (20% ถัดไป), OOS (20% สุดท้าย), Custom dates
เปอร์เซ็นต์คิดจาก `last_bar_index` จึงขยับเมื่อมีแท่งใหม่ → รายงานที่ต้องอ้างอิงได้ให้ล็อกด้วย **Custom dates** แล้วจดวันที่ไว้

ขั้นตอน:

1. Train: ปรับ parameter ได้ (ในกรอบข้อ 10.3)
2. Validation: **ห้ามปรับ** ดูว่า edge ยังอยู่ไหม ถ้าไม่ กลับไปข้อ 1 และ *บันทึกว่ากลับไปกี่รอบ*
3. OOS: รัน **ครั้งเดียว** ด้วย parameter จาก Train เท่านั้น ตัวเลข OOS ห้ามป้อนกลับไปปรับอะไร

### 10.2 Walk-forward

ใช้ Custom dates เลื่อนหน้าต่าง เช่น Train 3 เดือน → Test 1 เดือน แล้วเลื่อนทีละ 1 เดือน อย่างน้อย 4 window
รวมผล Test ทุก window เป็น walk-forward equity แล้วเทียบกับ Train

### 10.3 Robustness (Phase 13)

ต้องรันครบตาราง ไม่เลือกค่าที่ดีที่สุดค่าเดียว:

| มิติ | ค่าที่ต้องลอง |
|---|---|
| Timeframe | 1m, 3m, 5m |
| Session | Asian only, London only, NY only (ปิด session อื่น) |
| TP (ATR mode) | 0.2, 0.3, 0.4, 0.5 ATR |
| TP (R mode) | 0.5, 0.75, 1.0 R |
| SL | Structure / ATR 0.8, 1.0, 1.2 / Hybrid |
| Threshold | 65, 70, 75, 80 |
| Anti-chase drop | 2.0, 2.5, 3.0 ATR |

ผ่าน = ค่าข้างเคียงยังมี expectancy บวกและ PF ใกล้เคียง ไม่ใช่มีจุดเดียวที่ดี

### 10.4 Overfitting rules

ห้าม: ปรับ parameter หลายตัวพร้อมกัน, เพิ่ม filter เพื่อลบ trade แพ้ทีละตัว, ดู net profit อย่างเดียว,
เลือกช่วงเวลาที่สวยที่สุด, ใช้ข้อมูลอนาคต, แก้ code ให้ historical signal ต่างจาก realtime
เป้าหมายคือ **ผลคงที่ข้ามช่วงเวลา** ไม่ใช่ equity curve ที่สวยที่สุด

## 11. Report templates (ยังไม่มีตัวเลข – กรอกจากการรันจริงเท่านั้น)

### 11.1 Backtest report

```
Symbol / TF / feed:            XAUUSD / __m / (OANDA | Pyth | …)
Window (custom dates):         ____-__-__ → ____-__-__   (Train | Validation | OOS)
Spread / commission / slippage: ____ / ____ / ____   Bar magnifier: yes|no
Parameters changed from default: (list)
Total positions / legs:        ____ / ____
Win rate / Profit factor:      ____ % / ____
Expectancy  ($ | R):           ____ / ____
MEDIAN profit per trade ($|R): ____ / ____
Net profit / Max drawdown:     ____ / ____
Avg win / Avg loss / Avg R:    ____ / ____ / ____
Median duration:               ____ bars ≈ ____ min
Longest losing streak:         ____
Avg trades per session:        ____
By session (ASIAN/LONDON/NY/OVERLAP): n / win% / net / PF / avgR each
By setup (PB/BD/RJ/MC):        n / win% / net / PF / avgR each
```

### 11.2 Out-of-sample report

```
Train  (60%) dates … → metrics (สรุปแถวเดียว)
Valid  (20%) dates … → metrics
OOS    (20%) dates … → metrics   (รันครั้งเดียว วันที่รัน: ____)
Train→Valid rounds before freeze: ____
Verdict: edge holds | degrades | fails   (เหตุผล)
```

### 11.3 Walk-forward report

```
Window # | Train dates | Test dates | Test trades | Test win% | Test PF | Test median R
1 | … | … | … | … | … | …
… (≥ 4 windows)
Combined test PF / expectancy: ____ / ____   vs Train: ____ / ____
```

### 11.4 Robustness report

```
Parameter | value | trades | win% | PF | median R | note
TP ATR    | 0.2   | …
TP ATR    | 0.3   | …
… (ครบทุกแถวในข้อ 10.3)
Stable across: (TF | session | TP | SL | threshold)   Unstable at: ____
```

## 11.5 ผลรันจริงครั้งแรก (observed, 2026-09-04) – ยังไม่ใช่ OOS

รันบน TradingView ของผู้ใช้ผ่าน Chrome (feed **Pyth** = ราคา mid, `Chart price is MID` ยังปิดอยู่), parameter default ทุกตัว,
1 contract, commission 0.125/contract/order, slippage 2 ticks, ไม่มี Bar Magnifier, จำนวนแท่งที่โหลดได้ ≈ 6,600 ต่อ chart
ทั้งสองไฟล์ **compile ผ่าน** โดยไม่แก้อะไร

| TF | ช่วงข้อมูล | trades | win% | PF | net (USD/oz) | max DD |
|---|---|---|---|---|---|---|
| 1m | 31 ส.ค. – 4 ก.ย. 2026 (≈ 4.6 วัน) | 119 | 37.0% | 0.81 | −21.2 | 39.7 |
| 3m | 17 ส.ค. – 4 ก.ย. 2026 (≈ 2.5 สัปดาห์) | 115 | 36.5% | 1.02 | +3.2 | 35.4 |
| 5m | 10 ส.ค. – 4 ก.ย. 2026 (≈ 3.5 สัปดาห์) | 115 | 46.1% | 1.31 | +55.2 | 35.7 |

รายละเอียด 3m จาก dashboard ของ strategy: expectancy 0.03 USD (−0.05R), **median profit 0** (ครึ่งหนึ่งจบที่ break-even),
avg win 3.7 / avg loss −2.08, median duration 1 แท่ง, losing streak ยาวสุด 8, 2.0 trades ต่อ session
แยก setup (3m): PULLBACK 65 trades PF 0.83 (−14.9) · BREAKDOWN 28 trades PF 1.49 (+20.2) · REJECTION 22 trades PF 0.91 (−2.0) ·
**MOMENTUM 0 trades** (เงื่อนไขเข้มเกินจนไม่เคย trigger)
แยก session (3m): Asian 35 trades 40% PF 1.21 (+9.3) · London 45 trades 40% PF 1.12 (+7.0) · New York 22 trades 27% PF 0.63 (−9.8) ·
Overlap 13 trades 31% PF 0.87 (−3.2) — ตรงข้ามกับ session score ที่ตั้งไว้ (NY 10, Asian 4) จึงยังไม่ควรเชื่อคะแนน session

**รอบสอง (1m, ข้อมูลเดิม 31 ส.ค. – 4 ก.ย.)** หลังเพิ่ม filter ต้นทุน `Min SL distance as a multiple of the spread`:

| การตั้งค่า | trades | win% | net USD |
|---|---|---|---|
| default (threshold 70, filter ปิด) | 121 | 36.4% | −24.1 |
| 1m profile: threshold 80 + risk ≥ 6× spread | 90 | 37.8% | −23.0 |

กรองเข้มขึ้นตัด trade ไป 1 ใน 4 แต่ผลไม่ดีขึ้น → บน 1m ปัญหาอยู่ที่ตัว setup/exit ไม่ใช่แค่จำนวน trade
และ TradingView แผน Basic โหลด 1m ได้แค่ ~6,600 แท่ง (4.6 วัน) จึง **ปรับจูนบน 1m ไม่ได้อย่างมีความหมาย** ทางเลือกคือ
ใช้ 5m เป็น signal แล้วใช้ 1m แค่จับจังหวะ หรือเพิ่ม higher-timeframe bias (ต้องใช้ `request.security` แบบ non-repaint
เหมือน `ema_rsi_atr_advisory.pine` ซึ่งเป็นการเปลี่ยน design ที่ต้องตัดสินใจก่อน) หรือ backtest 1m จากประวัติ MT5 ที่ยาวกว่าใน Python

**รอบสาม (เพิ่ม higher-timeframe bias + min risk 4× spread, ข้อมูลเดิม)** — เทียบก่อน/หลัง:

| TF | ก่อน (trades / win% / PF / net) | หลัง HTF (trades / win% / PF / net / max DD) |
|---|---|---|
| 1m | 121 / 36.4% / 0.79 / −24.1 | 87 / 34.5% / 0.99 / −0.8 / 29.2 |
| 3m | 115 / 36.5% / 1.02 / +3.2 | 102 / 35.3% / 0.87 / −19.5 / 57.8 |
| 5m | 115 / 46.1% / 1.31 / +55.2 | 86 / 51.2% / 1.59 / +69.5 / 26.3 |

1m และ 5m ดีขึ้น 3m แย่ลง (15m เป็น HTF ของทั้ง 1m และ 3m แต่ผลต่างกัน) → ยืนยันว่าตัวอย่าง 2–4 สัปดาห์ยังเป็น noise
ห้ามสรุปว่า HTF "ใช้ได้" จนกว่าจะผ่าน OOS; แต่ที่ 5m ทุกรอบเป็นบวกและ DD ลดลง จึงเป็น TF ที่ควรใช้เป็น signal หลัก

สรุปตามเกณฑ์ข้อ 10: **ยังไม่ผ่าน** — 1m ขาดทุน, 3m เท่าทุน, 5m บวกแต่กำไรเกือบทั้งหมดมาจาก 1–3 ก.ย. ช่วงเดียว
(equity แบนก่อนหน้านั้น) และทุกตัวเลขเป็น in-sample ช่วงสั้น ยังไม่มี OOS / walk-forward
สิ่งที่ข้อมูลชี้: ปรับ momentum setup ให้ trigger ได้, ทบทวน pullback บน 1m/3m, spread/BE ทำให้ trade จำนวนมากจบที่ −0.002

## 11.6 Live watch (observed, 2026-09-04 14:31–15:53 UTC+7, XAUUSD 1m Pyth)

Strategy "Graph" (เวอร์ชัน HTF) รันสดบน chart 1m อ่านรายการ trade จาก Strategy Tester ทุก 10 นาที:

| เวลา | ราคา | เหตุการณ์ |
|---|---|---|
| 14:31 | 4463 | baseline: trade ล่าสุด #87 (13:58 → SL 14:00, −2.99) |
| 14:42 – 15:02 | 4478 → 4485.5 | ตลาดวิ่งขึ้น +22, ระบบไม่ short (15m bullish) |
| 15:12 – 15:53 | 4480 → 4475 | ย่อช้า ๆ แต่ 15m ยังไม่ปิดใต้ EMA21, ไม่มี signal |

82 นาที **0 signal** เวอร์ชันก่อนหน้า (ไม่มี HTF) เคย short 81/82 ใส่ขาขึ้นแบบนี้ตอน 13:00–14:00 แล้วโดน SL/BE
การเงียบในช่วง 15m bullish คือพฤติกรรมที่ออกแบบไว้ ไม่ใช่หลักฐานว่ามี edge — ต้องดูช่วงที่ 15m เป็นขาลงจึงจะประเมิน entry ได้

## 12. Development phases → code

| Phase | อยู่ที่ |
|---|---|
| 1 Market structure | core: `ph/pl`, `sh1/sl1`, `bosDown/bosUp`, `structTrend` |
| 2 Bearish bias | `biasPts`, `biasLevel` |
| 3 Pullback | `pb*` |
| 4 Breakdown | `bd*` |
| 5 Rejection | `rj*` |
| 6 Momentum filter / setup D | `momScore`, `volState`, `mc*` |
| 7 Anti-chasing | `chase*` |
| 8 Quick TP | `quickTpLevel`, `tQuickHit`, `quickExitStall` |
| 9 Risk management | `slLevel`, `tpLevel`, `f_managedSl`, gates |
| 10 Alerts | `alert()` + `alertcondition()` ในแต่ละ front end |
| 11 Strategy backtest | `xauusd_short_scalper_strategy.pine` |
| 12 Walk-forward / OOS | `Backtest window` inputs + ข้อ 10 |
| 13 Robustness | ข้อ 10.3 (รันด้วยมือ) |

## 13. สิ่งที่มันไม่ใช่

- Indicator เป็น `indicator()` ส่งคำสั่งไม่ได้ Strategy เป็น backtest บน TradingView เท่านั้น ไม่เชื่อมกับ pipeline Telegram→XM / MT5 ของ repo นี้
- ไม่มี LONG signal และไม่ได้ออกแบบสำหรับ timeframe > 5m
- Signal ที่ผ่าน filter ยังคงเป็น probability ไม่ใช่การรับประกัน: **No trade is better than a low-quality short**
