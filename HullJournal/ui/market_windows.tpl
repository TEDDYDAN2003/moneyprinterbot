<section class="panel marketResearch" id="marketResearch" aria-labelledby="marketTitle" hidden>
  <header class="marketHead">
    <div><div class="eyebrow">Price research / Exness tick archive</div><h2 id="marketTitle">Volatility by session</h2>
      <p>When does the market move most? Compare the typical five-minute move in each window, then check whether your Hull setups actually benefited.</p></div>
    <div class="marketStamp"><strong>SEP — OCT 2026</strong><span>Historical quotes · not trade results</span></div>
  </header>
  <div class="openGrid" id="openingCards" aria-label="Three cash-market opening hours to investigate"></div>
  <div class="marketToolbar"><div class="scope" role="group" aria-label="Compare activity windows">
    <button type="button" id="broadWindows" aria-pressed="true">Full sessions</button><button type="button" id="cashWindows" aria-pressed="false">Opening hours</button></div>
    <div class="marketChoices"><label>Markets <select id="activityKind"><option value="">All measured</option><option value="Indices">Equity indices</option><option value="FX">FX pairs</option><option value="Metals">Gold</option></select></label><label>Measure <select id="activityMetric"><option value="range">Mid-price range</option><option value="spread">Bid–ask spread</option></select></label></div>
  </div>
  <p id="matrixSummary" class="matrixLead" aria-live="polite"></p>
  <div class="scroll" role="region" aria-label="Market research matrix; click a column to rank" tabindex="0"><table class="marketTable"><caption class="sr">Historical medians in basis points. The word peak marks the highest window for each market.</caption><thead><tr id="matrixHead"></tr></thead><tbody id="matrixBody"></tbody></table></div>
  <p class="marketCaption"><b>1 bp = 0.01% of price.</b> Select a column to rank markets or a symbol to inspect it. Swipe the table on a small screen. A row’s peak marks its widest range (or spread), not its most profitable session. <strong>Not ranked:</strong> 1HZ100V is a 24/7 synthetic with no home exchange session.</p>
  <section class="focusMarket" id="focusMarket" aria-labelledby="focusTitle">
    <div class="focusHead"><div><div class="eyebrow">Compare with the journal</div><h3 id="focusTitle">Market in focus</h3></div><label>Inspect market <select id="focusKey"></select></label></div>
    <p class="focusIdentity" id="focusIdentity"></p><p class="focusInsight" id="focusInsight"></p>
    <p class="focusSource" id="focusSource"></p>
    <div class="scroll" role="region" tabindex="0" aria-label="Reference price movement beside filtered journal results"><table class="focusTable"><thead><tr><th scope="col">Entry window</th><th scope="col" class="num">M5 range · bp</th><th scope="col" class="num">Spread · bp</th><th scope="col" class="num">M5 bars</th><th scope="col" class="num" id="focusUnit"></th><th scope="col" class="num">Net P/L</th><th scope="col" class="num">Avg net</th></tr></thead><tbody id="focusBody"></tbody></table></div>
    <div class="focusFoot"><p>For a strategy study, use <strong>Setups</strong>: ten tickets from one signal are not ten independent observations. No records means no evidence, not zero profit.</p><button type="button" class="btn" id="exportFocus">Export comparison CSV</button></div>
  </section>
  <details class="marketMethod"><summary>Method, session boundaries &amp; sources</summary>
    <div class="methodGrid">
      <div><h3>What was measured</h3><p>1 September–31 October 2026 (UTC), on the exact Exness symbols shown. Each five-minute bar must have a valid two-sided quote in all five minutes. Range = 10,000 × (highest mid − lowest mid) / first mid; mid = (bid + ask) / 2. Each cell is the median of its bar values, not an annualised volatility or a return.</p>
      <p>Spread uses the first valid quote in each minute: 10,000 × (ask − bid) / mid. Take the median of those five spreads, then the median across bars. This is a quoted cost benchmark, not fill slippage. No forward-filling or zero bars for closed markets. Fewer than 10 observed UTC dates or 100 complete bars is too thin to rank.</p></div>
      <div><h3>How the clock works</h3><p>Local weekdays: Sydney 08:00–17:00, Tokyo 09:00–18:00, London and New York each 08:00–17:00. Each bar gets one broad bucket: London + New York first; otherwise London or New York when active; otherwise Asia-Pacific (Tokyo or Sydney); otherwise Off-session. Off-session is retained in the comparison, not ranked.</p>
      <p>Opening hours are a separate cut: the first 60 minutes from 09:00 Tokyo, 09:00 Frankfurt or 09:30 New York. City DST is applied per date; these are weekday clock windows, not holiday calendars or CFD dealing hours. Tokyo cash breaks 11:30–12:30 local. Opening-hour bars are not added to session totals.</p></div>
      <div><h3>Read the evidence</h3><p>The quote benchmark is fixed. Account, size, time frame and filters affect only the journal side. The comparison CSV records the observed entry dates, count, win rate and net results; these need not fall inside the reference period. Session comparison uses the actual opening fill (first fill for a setup), not the signal or exit. Synthetic sample rows are labelled. Use several months of out-of-sample setups before drawing conclusions; this is not a trading rule.</p>
      <p><strong>1HZ100V</strong> is a 24/7 synthetic, not an exchange index. There is no home cash session or Exness range estimate here. <strong>DE40 / DE30</strong> share one DE30m series; never count them twice.</p></div>
      <div><h3>Sources &amp; reproducibility</h3><ul>
        <li><a href="https://get.exness.help/hc/en-us/articles/360021547851-Tick-history" target="_blank" rel="noopener noreferrer">Exness: historical bid/ask quotes</a> · archive servers may differ from your demo feed.</li>
        <li><a href="https://get.exness.help/hc/en-us/articles/17854383867548-Indices" target="_blank" rel="noopener noreferrer">Exness index contracts</a> · <a href="https://www.jpx.co.jp/english/equities/trading/domestic/01.html" target="_blank" rel="noopener noreferrer">JPX cash hours</a> · <a href="https://www.deutsche-boerse.com/dbg-en/media/news-stories/press-releases/Deutsche-B-rse-Group-Launches-24-7-Trading-for-DAX-40-Equities-on-360X-5664632" target="_blank" rel="noopener noreferrer">Xetra core hours</a> · <a href="https://www.nyse.com/trade/hours-calendars" target="_blank" rel="noopener noreferrer">NYSE cash hours</a>.</li>
        <li><a href="https://www.rba.gov.au/publications/bulletin/2007/dec/1.html" target="_blank" rel="noopener noreferrer">RBA: intraday FX volatility</a> · historical cross-check, not the source of these medians. <a href="https://deriv.com/derived-indices/volatility-indices" target="_blank" rel="noopener noreferrer">Deriv: synthetic indices</a>.</li>
        </ul><p>In the bot repository: <code>research/session_volatility/</code> contains the calculation, archive URLs and hashes, and the full results CSV. This page does not download quotes or change the scanner.</p></div>
    </div>
  </details>
</section>
