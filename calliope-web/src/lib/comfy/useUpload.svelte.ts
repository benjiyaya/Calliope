/**
 * Shared upload logic — extracted from ComfyDynamicForm so both the legacy
 * form and OmniComposer can use it without duplication.
 *
 * Provides a Svelte 5 `$state`-based upload manager that handles:
 *   - file picker (click to browse)
 *   - drag-and-drop
 *   - upload progress per nodeId
 *   - metadata (name, kind) for uploaded files
 */
import { playgroundApi, type UploadKind } from '$lib/api';
import { toast } from '$lib/toast';

export interface UploadState {
	/** nodeId → uploading filename (present while upload is in-flight) */
	uploading: Record<string, string>;
	/** path → { name, kind } for previously uploaded files */
	uploadedMeta: Record<string, { name: string; kind: UploadKind }>;
}

export function createUploadManager() {
	let uploading = $state<Record<string, string>>({});
	let uploadedMeta = $state<Record<string, { name: string; kind: UploadKind }>>({});

	return {
		get uploading() {
			return uploading;
		},
		get uploadedMeta() {
			return uploadedMeta;
		},

		isUploading(nodeId: string): boolean {
			return !!uploading[nodeId];
		},

		uploadMetaFor(path: string): { name: string; kind: UploadKind } {
			const meta = uploadedMeta[path];
			if (meta) return meta;
			const base = path.split(/[/\\]/).pop() ?? path;
			const ext = base.slice(base.lastIndexOf('.')).toLowerCase();
			if (['.mp4', '.webm', '.mov', '.mkv'].includes(ext))
				return { name: base, kind: 'video' };
			if (['.mp3', '.wav', '.flac', '.ogg', '.m4a'].includes(ext))
				return { name: base, kind: 'audio' };
			return { name: base, kind: 'image' };
		},

		/**
		 * Upload a file and return the server path.
		 * Manages uploading state and metadata internally.
		 * Throws on failure (caller handles toast if needed).
		 */
		async upload(nodeId: string, file: File): Promise<string> {
			uploading = { ...uploading, [nodeId]: file.name };
			try {
				const res = await playgroundApi.upload(file);
				uploadedMeta = {
					...uploadedMeta,
					[res.path]: { name: res.name, kind: res.kind },
				};
				return res.path;
			} finally {
				const next = { ...uploading };
				delete next[nodeId];
				uploading = next;
			}
		},

		/**
		 * Convenience: upload + toast on error, return path or null.
		 */
		async uploadSafe(nodeId: string, file: File): Promise<string | null> {
			try {
				return await this.upload(nodeId, file);
			} catch (err) {
				toast.error(err instanceof Error ? err.message : 'Upload failed');
				return null;
			}
		},
	};
}

export type UploadManager = ReturnType<typeof createUploadManager>;

export function truncateMiddle(name: string, max = 32): string {
	if (name.length <= max) return name;
	const head = Math.ceil((max - 1) / 2);
	const tail = Math.floor((max - 1) / 2);
	return `${name.slice(0, head)}…${name.slice(name.length - tail)}`;
}

export function acceptForKind(kind: string): string {
	if (kind === 'audio') return 'audio/*';
	if (kind === 'video') return 'video/*,.mp4,.webm,.mov,.mkv';
	return 'image/*';
}
