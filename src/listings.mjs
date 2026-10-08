// Conservative adapter: importing upstream is disabled until its schema is verified.
// Only this explicit, versioned normalized interchange format is accepted.
export function parseListings(data) {
  if (!data || data.schema !== 'copilot-listings-v1' || !Array.isArray(data.listings)) return [];
  const results = [];
  const seen = new Set();
  for (const item of data.listings) {
    if (results.length >= 100) break;
    if (!item || !['UK', 'United Kingdom'].includes(item.country) || item.sector !== 'tech' || typeof item.company !== 'string' || !item.company.trim() || typeof item.role !== 'string' || !item.role.trim()) continue;
    let url;
    try { url = new URL(item.url); } catch { continue; }
    if (url.protocol !== 'https:' || url.username || url.password) continue;
    if (seen.has(url.href)) continue;
    seen.add(url.href);
    results.push({ company: item.company.trim(), role: item.role.trim(), sector: 'tech', location: typeof item.location === 'string' ? item.location : 'United Kingdom', url: url.href, companyUrl: '', jobDescription: '', questions: [], provenance: { source: 'user-supplied normalized listing; not verified open', url: url.href } });
  }
  return results;
}
