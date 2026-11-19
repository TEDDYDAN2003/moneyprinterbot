# Optional, offline calculation parity

No QA dependency is needed to compile the indicator. To repeat the offline checks on a development machine with Node, Python 3.11+ and `g++`, from the `MT5` folder:

```sh
cd qa && npm ci --ignore-scripts && cd ..
node qa/pine_oracle.cjs qa/candles.json qa/reference.pine /tmp/hull-oracle.json
python qa/check_port.py /tmp/hull-oracle.json
```

`candles.json` is synthetic. The oracle executes the supplied Pine with PineTS and exports 40 chart-state fields. The second command tests the delivered `.mq5` after mechanical array/builtin adaptation to C++ and uses stubbed terminal plotting calls for lifecycle checks. It does not compile an EX5 or test the appearance of objects in MT5. For a native check, see `INSTALL-MT5.md`.
