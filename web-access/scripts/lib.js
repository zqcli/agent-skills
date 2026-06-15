import { Readability } from "@mozilla/readability";
import { parseHTML } from "linkedom";
import TurndownService from "turndown";
import { gfm } from "turndown-plugin-gfm";
import { fetch, ProxyAgent } from "undici";

const UA =
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36";

// Use undici's own fetch + dispatcher so the proxy agent always matches the
// fetch implementation (avoids version skew with Node's bundled undici).
function proxyDispatcher(proxy) {
	const url = proxy || process.env.HTTPS_PROXY || process.env.HTTP_PROXY;
	return url ? new ProxyAgent(url) : undefined;
}

export async function httpGet(url, { proxy, timeoutMs = 15000, accept } = {}) {
	return fetch(url, {
		headers: {
			"User-Agent": UA,
			Accept: accept ?? "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
			"Accept-Language": "en-US,en;q=0.9",
		},
		dispatcher: proxyDispatcher(proxy),
		signal: AbortSignal.timeout(timeoutMs),
	});
}

const turndown = new TurndownService({ headingStyle: "atx", codeBlockStyle: "fenced" });
turndown.use(gfm);
turndown.addRule("dropEmptyLinks", {
	filter: (node) => node.nodeName === "A" && !node.textContent?.trim(),
	replacement: () => "",
});

export function htmlToMarkdown(html) {
	return turndown
		.turndown(html)
		.replace(/\[\\?\[\s*\\?\]\]\([^)]*\)/g, "")
		.replace(/ +/g, " ")
		.replace(/\n{3,}/g, "\n\n")
		.trim();
}

const BOILERPLATE = "script, style, noscript, nav, header, footer, aside";
const MAIN_REGION = "main, article, [role='main'], .content, #content";

// Readability mutates the document during parse, so the fallback re-parses.
export function extractReadable(html, url) {
	const article = new Readability(parseHTML(html).document).parse();
	if (article?.content) {
		return { title: article.title ?? "", markdown: htmlToMarkdown(article.content) };
	}

	const { document } = parseHTML(html);
	for (const el of document.querySelectorAll(BOILERPLATE)) el.remove();
	const region = document.querySelector(MAIN_REGION) ?? document.body;
	const markdown = region ? htmlToMarkdown(region.innerHTML) : "";
	if (markdown.length < 80) return null;
	return { title: document.querySelector("title")?.textContent?.trim() ?? "", markdown };
}

export async function braveSearch({ query, count = 5, country = "US", freshness, proxy }) {
	const key = process.env.BRAVE_API_KEY;
	if (!key) throw new Error("BRAVE_API_KEY is not set. Get one at https://api-dashboard.search.brave.com");

	const params = new URLSearchParams({ q: query, count: String(Math.min(count, 20)), country });
	if (freshness) params.set("freshness", freshness);

	const res = await fetch(`https://api.search.brave.com/res/v1/web/search?${params}`, {
		headers: { Accept: "application/json", "Accept-Encoding": "gzip", "X-Subscription-Token": key },
		dispatcher: proxyDispatcher(proxy),
		signal: AbortSignal.timeout(15000),
	});
	if (!res.ok) throw new Error(`Brave API ${res.status} ${res.statusText}: ${await res.text()}`);

	const data = await res.json();
	return (data.web?.results ?? []).slice(0, count).map((r) => ({
		title: r.title ?? "",
		url: r.url ?? "",
		age: r.age || r.page_age || "",
		// Brave wraps query matches in <strong>; drop tags for clean text.
		snippet: (r.description ?? "").replace(/<[^>]+>/g, ""),
	}));
}
