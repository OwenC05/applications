// This preview deliberately edits only writing/text questions. Never silently
// replace discovered controls, optionality or existing per-question limits.
export function writingQuestions(text, previous = [], defaults = {}, newId = () => `q_${crypto.randomUUID()}`) {
  if (previous.some(question => !['writing', 'text'].includes(question.type))) {
    throw new Error('This basic editor cannot replace sensitive, option or file controls. Use manual handling until the form editor is qualified.');
  }
  if (previous.some(question => question.text.includes('\n'))) {
    throw new Error('This line-based preview cannot edit existing multiline questions without changing their boundaries. Use manual handling until the full question editor is qualified.');
  }
  const limits = {};
  for (const [key, value] of Object.entries(defaults)) {
    if (value === '') continue;
    const number = Number(value);
    if (!Number.isSafeInteger(number) || number < 1) throw new Error('Question limits must be positive whole numbers.');
    limits[key] = number;
  }
  const lines = text.split('\n').map(line => line.trim()).filter(Boolean);
  if (previous.length && (lines.length !== previous.length || lines.some((line, index) => line !== previous[index].text))) {
    throw new Error('Existing questions must be edited by stable IDs. This line-based preview cannot change their text, order or boundaries.');
  }
  return lines.map((wording, index) => {
    const old = previous[index];
    const changed = old && Object.keys(limits).some(key => limits[key] !== old[key]);
    return {
      ...(old || {schema_version: 1, id: newId(), type: 'writing', required: true, max_words: null, max_chars: null, options: []}),
      text: wording,
      ...limits,
      constraint_origin: old && !changed ? old.constraint_origin : 'user',
    };
  });
}

export function sourcePreview(units, maximum = 12000) {
  let remaining = maximum;
  const parts = [];
  let limited = false;
  for (const unit of units) {
    if (!remaining) { limited = true; break; }
    const points = Array.from(unit.text);
    const excerpt = points.slice(0, remaining);
    parts.push(`${unit.page ? `Physical PDF page ${unit.page}` : 'Canonical text'}\n${excerpt.join('')}`);
    remaining -= excerpt.length;
    if (excerpt.length < points.length) limited = true;
  }
  if (limited) parts.push(`[Preview limited to ${maximum.toLocaleString('en-GB')} canonical code points across all units; original remains available.]`);
  return parts.join('\n\n');
}
