/** Shared shape for Omni / video ref pickers. */
export type AssetGroup = 'character' | 'location' | 'item' | 'upload' | 'clip';

export interface AssetOption {
	label: string;
	path: string;
	kind?: 'image' | 'video' | 'audio';
	group?: AssetGroup;
}

const GROUP_FROM_SUFFIX: Array<{ re: RegExp; group: AssetGroup }> = [
	{ re: /\s·\s*sheet$/i, group: 'character' },
	{ re: /\s·\s*environment$/i, group: 'location' },
	{ re: /\s·\s*item$/i, group: 'item' },
	{ re: /\s·\s*upload$/i, group: 'upload' },
	{ re: /^clip\s*#/i, group: 'clip' },
];

export function assetGroup(opt: AssetOption): AssetGroup {
	if (opt.group) return opt.group;
	for (const { re, group } of GROUP_FROM_SUFFIX) {
		if (re.test(opt.label)) return group;
	}
	return 'upload';
}

/** Strip the " · sheet" style suffix so the tab already names the type. */
export function assetDisplayName(opt: AssetOption): string {
	return opt.label.replace(/\s·\s*(sheet|environment|item|upload)\s*$/i, '').trim() || opt.label;
}

export const ASSET_GROUP_TABS: Array<{ id: AssetGroup; label: string }> = [
	{ id: 'character', label: 'Characters' },
	{ id: 'location', label: 'Environments' },
	{ id: 'item', label: 'Misc. Items' },
	{ id: 'clip', label: 'Clips' },
	{ id: 'upload', label: 'Uploads' },
];

export function tabsForMediaKind(kind: string | undefined): Array<{ id: AssetGroup; label: string }> {
	const ids: AssetGroup[] =
		kind === 'video'
			? ['clip', 'upload']
			: kind === 'audio'
				? ['upload']
				: ['character', 'location', 'item', 'upload'];
	return ASSET_GROUP_TABS.filter((t) => ids.includes(t.id));
}
