#!/usr/bin/env node
import { httpGet, extractReadable } from "./lib.js";

const USAGE = `Usage: read.js <url> [options]
  --raw            Print the raw response body (for JSON / non-HTML APIs)
  --max-chars <n>  Truncate output (default: no limit)
  --timeout <sec>  Request timeout in seconds (default 15)
  --proxy <url>    http(s) proxy (or HTTPS_PROXY env)`;

function parseArgs(argv) {
	const opts = { maxChars: 0, timeoutMs: 15000 };
	const rest = [];
	for (let i = 0; i < argv.length; i++) {
		const a = argv[i];
		if (a === "--raw") opts.raw = true;
		else if (a === "--proxy") opts.proxy = argv[++i];
		else if (a === "--max-chars") opts.maxChars = parseInt(argv[++i], 10);
		else if (a === "--timeout") opts.timeoutMs = parseInt(argv[++i], 10) * 1000;
		else rest.push(a);
	}
	opts.url = rest[0];
	return opts;
}

const opts = parseArgs(process.argv.slice(2));
if (!opts.url) {
	console.error(USAGE);
	process.exit(1);
}

try {
	const res = await httpGet(opts.url, { proxy: opts.proxy, timeoutMs: opts.timeoutMs });
	if (!res.ok) {
		console.error(`HTTP ${res.status} ${res.statusText}`);
		process.exit(1);
	}

	const body = await res.text();
	const contentType = res.headers.get("content-type") ?? "";
	const isHtml = contentType.includes("text/html") || contentType.includes("xml");

	let output;
	if (opts.raw || !isHtml) {
		output = body;
	} else {
		const article = extractReadable(body, opts.url);
		if (!article) {
			console.error("Could not extract readable content. Retry with --raw.");
			process.exit(1);
		}
		output = article.title ? `# ${article.title}\n\n${article.markdown}` : article.markdown;
	}

	console.log(opts.maxChars > 0 ? output.slice(0, opts.maxChars) : output);
} catch (e) {
	console.error(`Error: ${e.message}`);
	process.exit(1);
}
