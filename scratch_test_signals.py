# Quick simulation test of signal tags logic
class Event:
    def __init__(self, closedAt, code):
        self.closedAt = closedAt
        self.eventCode = code

class Bar:
    def __init__(self, time, low, high):
        self.time = time
        self.timeClose = time + 900
        self.low = low
        self.high = high

events = [
    Event(1000, 2),   # Bullish MSS at 1000
    Event(2500, 1),   # Bullish BOS at 2500
    Event(4000, -2),  # Bearish MSS at 4000
    Event(5500, -1),  # Bearish BOS at 5500
]

bars = [
    Bar(900, 100, 105),   # before MSS
    Bar(1200, 102, 107),  # buy signal 1 -> should be BUY 1
    Bar(1800, 105, 110),  # buy signal 2 -> should be BUY
    Bar(2700, 110, 115),  # buy signal 3 after BOS -> should be BUY
    Bar(4200, 108, 112),  # sell signal 1 after bearish MSS -> should be SELL 1
    Bar(4800, 104, 108),  # sell signal 2 -> should be SELL
    Bar(5800, 100, 104),  # sell signal 3 after BOS -> should be SELL
]

evtPtr = 0
lastMssBias = 0
lastMssSeenSig = True

results = []
for b in bars:
    while evtPtr < len(events) and events[evtPtr].closedAt <= b.timeClose:
        ev = events[evtPtr].eventCode
        if abs(ev) == 2:
            lastMssBias = 1 if ev > 0 else -1
            lastMssSeenSig = False
        evtPtr += 1
    
    # check signals
    if b.time in [1200, 1800, 2700]:
        # buy signal (bias = 1)
        if lastMssBias == 1 and not lastMssSeenSig:
            tag = "BUY 1"
            lastMssSeenSig = True
        else:
            tag = "BUY"
        results.append((b.time, tag))
    elif b.time in [4200, 4800, 5800]:
        # sell signal (bias = -1)
        if lastMssBias == -1 and not lastMssSeenSig:
            tag = "SELL 1"
            lastMssSeenSig = True
        else:
            tag = "SELL"
        results.append((b.time, tag))

for t, tag in results:
    print(f'Time {t}: {tag}')

assert results == [
    (1200, 'BUY 1'),
    (1800, 'BUY'),
    (2700, 'BUY'),
    (4200, 'SELL 1'),
    (4800, 'SELL'),
    (5800, 'SELL'),
]
print('ALL ASSERTIONS PASSED PERFECTLY!')
