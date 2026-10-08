// Deliberately excludes interview transcripts and application-history prose.
export function buildContext(profile, application) {
  const stopwords = new Set(['the', 'and', 'for', 'you', 'your', 'with', 'our', 'are', 'this', 'that', 'will', 'from', 'have', 'has', 'not', 'but', 'can', 'any', 'who', 'how', 'what', 'why']);
  const researchTerms = [...(application.research?.requirements || []), ...(application.research?.hiringPriorities || [])].map(item => item.text).join(' ');
  const terms = new Set((`${researchTerms} ${application.role} ${application.sector} ${application.jobDescription} ${(application.questions || []).map(item => item.text).join(' ')}`.toLowerCase().match(/[a-z0-9]{3,}/g) || []).filter(term => !stopwords.has(term)));
  const evidence = (profile.memories || []).filter(memory => memory.status === 'verified')
    .map((memory, index) => ({ memory, index, score: [...terms].filter(term => `${memory.category} ${memory.label} ${memory.content}`.toLowerCase().includes(term)).length }))
    .filter(item => item.score > 0)
    .sort((a, b) => b.score - a.score || a.index - b.index).slice(0, 24)
    .map(({ memory }) => ({ id: memory.id, category: memory.category, label: memory.label, content: memory.content }));
  return {
    role: { company: application.company, role: application.role, sector: application.sector, location: application.location || '', jobDescription: application.jobDescription, questions: (application.questions || []).map(item => ({ id: item.id, text: item.text, maxWords: item.maxWords ?? null, maxChars: item.maxChars ?? null })) },
    research: application.research ? { summary: application.research.summary, companyFacts: application.research.companyFacts, requirements: application.research.requirements, hiringPriorities: application.research.hiringPriorities, sources: application.research.sources } : null,
    evidence, writingPreferences: profile.writingPreferences || '',
  };
}
