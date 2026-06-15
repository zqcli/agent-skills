#!/usr/bin/env node
import { braveSearch, httpGet, extractReadable } from "./lib.js";

const USAGE = `Usage: search.js <query> [options]
  -n, --count <num>     Number of results (default 5, max 20)
  --country <code>      Two-letter country code (default US)
  --freshness <period>  pd | pw | pm | py | YYYY-MM-DDtoYYYY-MM-DD
  --content             Fetch each result page as readable markdown
  --max-chars <n>       Truncate per-page content (default 4000)
  --proxy <url>         http(s) proxy (or HTTPS_PROXY env)
Env: BRAVE_API_KEY (required)`;

function parseArgs(argv) {
	const opts = { count: 5, country: "US", content: false, maxChars: 4000 };
	const rest = [];
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i];
		if (a === "-n" || a === "--count") opts.count = parseInt(argv[++i], 10);
		else if (a === "--country") opts.country = argv[++i].toUpperCase();
		else if (a === "--freshness") opts.freshness = argv[++i];
		else if (a === "--proxy") opts.proxy = argv[++i];
		else if (a === "--max-chars") opts.maxChars = parseInt(argv[++i], 10);
		else if (a === "--content") opts.content = true;
		else rest.push(a);
	}
	opts.query = rest.join(" ");
	return opts;
}

const opts = parseArgs(process.argv.slice(2));
if (!opts.query) {
	console.error(USAGE);
	process.exit(1);
}

try {
	const results = await braveSearch(opts);
	if (results.length === 0) {
		console.log("No results.");
		process.exit(0);
	}

	// Serial fetch keeps within Brave/site rate limits.
	if (opts.content) {
		for (const r of results) {
			try {
				const res = await httpGet(r.url, { proxy: opts.proxy, timeoutMs: 10000 });
				const article = res.ok ? extractReadable(await res.text(), r.url) : null;
				r.content = article ? article.markdown.slice(0, opts.maxChars) : `(HTTP ${res.status})`;
			} catch (e) {
				r.content = `(error: ${e.message})`;
			}
		}
	}

	const blocks = results.map((r, i) => {
		const head = `## ${i + 1}. ${r.title}\n${r.url}${r.age ? ` · ${r.age}` : ""}\n${r.snippet}`;
		return r.content ? `${head}\n\n${r.content}` : head;
	});
	console.log(blocks.join("\n\n---\n\n"));
} catch (e) {
	console.error(`Error: ${e.message}`);
	process.exit(1);
}
