// NODE_PATH=<MT5/qa/node_modules> node tests/wickless_qa/parity.cjs fixture.json
const fs = require("node:fs"),
  assert = require("node:assert/strict"),
  path = require("node:path");
const { PineTS, Indicator } = require("pinets");
const src = fs.readFileSync(
  path.resolve(__dirname, "../../indicators/Wickless-M15-Rejection.pine"),
  "utf8",
);
const keys = [
  "bias",
  "event",
  "guard",
  "raw",
  "armed",
  "setupStop",
  "ds.size()",
  "signal",
  "origin",
  "stop",
  "target",
];
const script =
  src + "\n" + keys.map((k, i) => `plot(${k},"qa${i}")`).join("\n");
const sym = {
  ticker: "FIX",
  tickerid: "QA:FIX",
  mintick: 0.01,
  pricescale: 100,
  minmove: 1,
  pointvalue: 1,
  timezone: "Etc/UTC",
  type: "forex",
  session: "24x7",
};
async function run(bars, opts, live = false, tf = "15") {
  const shift = live
    ? Math.floor(Date.now() / 900000) * 900000 - bars.at(-1)[0] * 1000
    : 0;
  const data = bars.map(([t, o, h, l, c]) => ({
    openTime: t * 1000 + shift,
    closeTime: (t + 900) * 1000 + shift,
    open: o / 100,
    high: h / 100,
    low: l / 100,
    close: c / 100,
    volume: 1,
  }));
  const engine = new PineTS(
    {
      getMarketData: async () => data,
      getSymbolInfo: async () => sym,
      configure() {},
    },
    "QA:FIX",
    tf,
    data.length,
  );
  engine.setAlertMode("all");
  return engine.run(new Indicator(script, opts));
}
(async () => {
  let total = 0,
    buy = 0,
    sell = 0;
  for (const { bars, rows, opts } of JSON.parse(
    fs.readFileSync(process.argv[2], "utf8"),
  )) {
    const ctx = await run(bars, opts);
    for (let i = 0; i < rows.length; i++)
      for (let j = 0; j < keys.length; j++) {
        const v = ctx.plots[`qa${j}`].data[i].value;
        assert.equal(
          v == null || Number.isNaN(v) ? null : v,
          rows[i][j],
          `${i} ${keys[j]} expected ${rows[i][j]} got ${v}`,
        );
      }
    for (const [side, title] of [
      [1, "BUY · respected"],
      [-1, "SELL · respected"],
    ]) {
      assert.deepEqual(
        ctx.plots[title].data.map((k) => !!k.value),
        rows.map((k) => k[7] === side),
      );
    }
    if (total === 0) {
      for (const dir of [1, -1]) {
        const i = rows.findIndex((x) => x[7] === dir);
        assert(i > 0);
        const forming = await run(bars.slice(0, i + 1), opts, true);
        assert.equal(
          forming.plots.qa7.data[i].value,
          0,
          "no suggestion on a forming rejection candle",
        );
        assert.equal(
          forming.plots.qa4.data[i].value,
          0,
          "no setup on an incomplete wickless candle",
        );
      }
      await assert.rejects(
        () => run(bars.slice(0, 50), opts, false, "5"),
        /15.minute|15-minute/,
      );
    }
    buy += rows.filter((k) => k[7] === 1).length;
    sell += rows.filter((k) => k[7] === -1).length;
    total += rows.length;
  }
  assert(buy > 0 && sell > 0);
  console.log(
    `PASS ${total} closed M15 rows × ${keys.length} numeric fields, both plotted entry sides (${buy} BUY, ${sell} SELL).`,
  );
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
