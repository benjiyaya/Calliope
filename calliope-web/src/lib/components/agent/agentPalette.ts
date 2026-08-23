/**
 * Agent identity palette: a stable display name + accent color per agent.
 *
 * The main loop runs with `agent_name = null`; we label it "Calliope". Each
 * swarm sub-agent ("planner", "story-agent", …) gets its own fixed color so
 * differently-authored bubbles are visually distinct, plus a deterministic
 * fallback for any future agent name.
 */

export const DEFAULT_AGENT_NAME = 'Calliope';

// Fixed identity map (keyed by the raw agent_name the backend emits).
const IDENTITIES: Record<string, { label: string; color: string }> = {
	planner: { label: 'Planner', color: '#22d3ee' }, // cyan
	'story-agent': { label: 'Story Agent', color: '#22c55e' }, // green
	'script-agent': { label: 'Script Agent', color: '#f59e0b' }, // amber
	'assets-agent': { label: 'Assets Agent', color: '#ec4899' }, // pink
	'video-agent': { label: 'Video Agent', color: '#fb923c' }, // orange
	'research-agent': { label: 'Research Agent', color: '#a78bfa' }, // violet
};

// Fallback palette for unknown names (stable hash → deterministic color).
const FALLBACK_COLORS = [
	'#22d3ee',
	'#22c55e',
	'#f59e0b',
	'#ec4899',
	'#fb923c',
	'#a78bfa',
	'#38bdf8',
	'#4ade80',
	'#f472b6',
	'#facc15',
];

function hashString(s: string): number {
	let h = 0;
	for (let i = 0; i < s.length; i++) {
		h = (h * 31 + s.charCodeAt(i)) >>> 0;
	}
	return h;
}

/** The default "Calliope" accent (matches --accent). */
export const DEFAULT_AGENT_COLOR = '#8b5cf6';

export function agentDisplayName(name: string | null | undefined): string {
	const key = (name || '').trim().toLowerCase();
	if (!key) return DEFAULT_AGENT_NAME;
	return IDENTITIES[key]?.label ?? prettyName(name!);
}

export function agentColor(name: string | null | undefined): string {
	const key = (name || '').trim().toLowerCase();
	if (!key) return DEFAULT_AGENT_COLOR;
	const identity = IDENTITIES[key];
	if (identity) return identity.color;
	return FALLBACK_COLORS[hashString(key) % FALLBACK_COLORS.length];
}

function prettyName(raw: string): string {
	// "story-agent" → "Story Agent", "assets-agent" → "Assets Agent"
	const title = raw
		.split(/[-_\s]+/)
		.filter(Boolean)
		.map((w) => w.charAt(0).toUpperCase() + w.slice(1))
		.join(' ');
	return title || DEFAULT_AGENT_NAME;
}
