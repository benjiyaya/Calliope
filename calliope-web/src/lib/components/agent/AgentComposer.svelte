<script lang="ts">
	import {
		MAX_WORKFLOW_MENTIONS,
		filterSkills,
		filterWorkflows,
		payloadIsEmpty,
		type AgentAttachment,
		type AgentComposerPayload,
		type AttachmentKind,
		type SkillMention,
		type SkillOption,
		type WorkflowMention,
		type WorkflowOption,
	} from '$lib/agentComposer';
	import { assetUrl } from '$lib/api';
	import Icon from '$lib/components/ui/Icon.svelte';
	import { createUploadManager, truncateMiddle } from '$lib/comfy/useUpload.svelte';
	import { toast } from '$lib/toast';
	import WorkflowMentionMenu from './WorkflowMentionMenu.svelte';

interface Props {
	running: boolean;
	onSend: (payload: AgentComposerPayload) => void;
	onCancel: () => void;
	/** Text to pre-fill into the composer (deep-link handoff or a suggestion). */
	draft?: string;
	/** Bumps to re-apply `draft` (including the empty string) and refocus. */
	draftNonce?: number;
	/** Enabled ComfyUI workflows for the `@` typeahead. */
	workflows?: WorkflowOption[];
	/** Available skills for the `/` typeahead. */
	skills?: SkillOption[];
}

let {
	running,
	onSend,
	onCancel,
	draft = '',
	draftNonce = 0,
	workflows = [],
	skills = [],
}: Props = $props();

	const ATTACH_LIMIT = 8;
	const ACCEPT = 'image/*,video/*,audio/*,.png,.jpg,.jpeg,.webp,.gif,.mp4,.webm,.mov,.mkv,.mp3,.wav,.flac,.ogg,.m4a';

	let editorEl = $state<HTMLDivElement | null>(null);
	let fileInput = $state<HTMLInputElement | null>(null);
	let editorEmpty = $state(true);
	let dragOver = $state(false);
	let attachments = $state<AgentAttachment[]>([]);
	let uploadingNames = $state<string[]>([]);

	let mentionOpen = $state(false);
	let mentionQuery = $state('');
	let mentionIndex = $state(0);
	let mentionAnchor = $state({ top: 0, bottom: 0, left: 0 });
	let mentionLocked = $state(false);

	// `/` slash command state (skill picker). Same mechanics as the `@`
	// workflow menu — different trigger char and item list.
	let slashOpen = $state(false);
	let slashQuery = $state('');
	let slashIndex = $state(0);
	let slashAnchor = $state({ top: 0, bottom: 0, left: 0 });

	const uploads = createUploadManager();
	const mentionItems = $derived(filterWorkflows(workflows, mentionQuery));
	const slashItems = $derived(filterSkills(skills, slashQuery));
	const busy = $derived(running || uploadingNames.length > 0);
	const sendable = $derived(!busy && (!editorEmpty || attachments.length > 0));

	$effect(() => {
		void draftNonce;
		if (draftNonce === 0) return;
		if (!editorEl) return;
		editorEl.textContent = draft;
		placeCaretAtEnd(editorEl);
		editorEl.focus();
		syncEmpty();
	});

	$effect(() => {
		void mentionItems.length;
		if (mentionIndex >= mentionItems.length) mentionIndex = Math.max(0, mentionItems.length - 1);
	});

	$effect(() => {
		if (!mentionOpen && !slashOpen) return;
		function onDocDown(e: MouseEvent) {
			const t = e.target as Node | null;
			if (editorEl?.contains(t)) return;
			if (t instanceof Element && t.closest('.mention-menu')) return;
			mentionOpen = false;
			slashOpen = false;
		}
		document.addEventListener('mousedown', onDocDown);
		return () => document.removeEventListener('mousedown', onDocDown);
	});

	function syncEmpty() {
		if (!editorEl) {
			editorEmpty = true;
			return;
		}
		editorEmpty = editorEl.innerText.replace(/\u00a0/g, ' ').trim().length === 0
			&& !editorEl.querySelector('.wf-chip');
	}

	function serialize(): {
		content: string;
		mentions: (WorkflowMention | SkillMention)[];
	} {
		if (!editorEl) return { content: '', mentions: [] };
		const mentions: (WorkflowMention | SkillMention)[] = [];
		let content = '';
		const walk = (node: Node) => {
			if (node.nodeType === Node.TEXT_NODE) {
				content += node.textContent ?? '';
				return;
			}
			if (node instanceof HTMLElement && node.classList.contains('wf-chip')) {
				const id = Number(node.dataset.workflowId);
				const name = node.dataset.workflowName ?? node.textContent?.replace(/^@/, '') ?? '';
				const kind = node.dataset.workflowKind === 'video' ? 'video' : 'image';
				if (Number.isFinite(id) && name) {
					mentions.push({ type: 'workflow', id, name, kind });
				}
				content += `@${name}`;
				return;
			}
			if (node instanceof HTMLElement && node.classList.contains('skill-chip')) {
				const name = node.dataset.skillName ?? node.textContent?.replace(/^\//, '') ?? '';
				const description = node.dataset.skillDescription ?? '';
				if (name) {
					mentions.push({ type: 'skill', name, description });
				}
				content += `/${name}`;
				return;
			}
			if (node instanceof HTMLElement && (node.tagName === 'BR' || node.tagName === 'DIV' || node.tagName === 'P')) {
				if (node !== editorEl && content.length > 0 && !content.endsWith('\n')) content += '\n';
			}
			for (const child of Array.from(node.childNodes)) walk(child);
		};
		walk(editorEl);
		return {
			content: content.replace(/\u00a0/g, ' ').replace(/\n+$/, '').trim(),
			mentions: mentions.slice(0, MAX_WORKFLOW_MENTIONS + 4),
		};
	}

	function submit() {
		if (busy) return;
		const { content, mentions } = serialize();
		const payload: AgentComposerPayload = {
			content,
			mentions,
			attachments: [...attachments],
		};
		if (payloadIsEmpty(payload)) return;
		if (editorEl) editorEl.innerHTML = '';
		attachments = [];
		mentionOpen = false;
		mentionLocked = false;
		slashOpen = false;
		syncEmpty();
		onSend(payload);
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.isComposing || e.keyCode === 229) return;
		if (mentionOpen) {
			if (mentionLocked && (e.key === 'Enter' || e.key === 'Tab')) {
				e.preventDefault();
				mentionOpen = false;
				mentionLocked = false;
				return;
			}
			if (e.key === 'ArrowDown') {
				e.preventDefault();
				mentionIndex = mentionItems.length === 0 ? 0 : (mentionIndex + 1) % mentionItems.length;
				return;
			}
			if (e.key === 'ArrowUp') {
				e.preventDefault();
				mentionIndex = mentionItems.length === 0
					? 0
					: (mentionIndex - 1 + mentionItems.length) % mentionItems.length;
				return;
			}
			if (e.key === 'Enter' || e.key === 'Tab') {
				e.preventDefault();
				const wf = mentionItems[mentionIndex];
				if (wf) insertMention(wf);
				else mentionOpen = false;
				return;
			}
			if (e.key === 'Escape') {
				e.preventDefault();
				mentionOpen = false;
				mentionLocked = false;
				return;
			}
		}
		if (slashOpen) {
			if (e.key === 'ArrowDown') {
				e.preventDefault();
				slashIndex = slashItems.length === 0 ? 0 : (slashIndex + 1) % slashItems.length;
				return;
			}
			if (e.key === 'ArrowUp') {
				e.preventDefault();
				slashIndex = slashItems.length === 0
					? 0
					: (slashIndex - 1 + slashItems.length) % slashItems.length;
				return;
			}
			if (e.key === 'Enter' || e.key === 'Tab') {
				e.preventDefault();
				const skill = slashItems[slashIndex];
				if (skill) insertSkill(skill);
				else slashOpen = false;
				return;
			}
			if (e.key === 'Escape') {
				e.preventDefault();
				slashOpen = false;
				return;
			}
		}
		if (e.key === 'Enter' && !e.shiftKey) {
			e.preventDefault();
			submit();
		}
	}

	function onPaste(e: ClipboardEvent) {
		e.preventDefault();
		const text = e.clipboardData?.getData('text/plain') ?? '';
		document.execCommand('insertText', false, text);
		onEditorInput();
	}

	function chipCount(): number {
		return editorEl?.querySelectorAll('.wf-chip').length ?? 0;
	}

	function onEditorInput() {
		syncEmpty();
		const slash = readTriggerQuery(editorEl, '/');
		if (slash) {
			slashQuery = slash.query;
			slashOpen = true;
			slashIndex = 0;
			slashAnchor = caretAnchor();
		} else {
			slashOpen = false;
		}
		const info = readTriggerQuery(editorEl, '@', '.skill-chip');
		if (!info) {
			mentionOpen = false;
			mentionLocked = false;
			return;
		}
		mentionQuery = info.query;
		mentionOpen = true;
		mentionIndex = 0;
		mentionAnchor = caretAnchor();
		mentionLocked = chipCount() >= MAX_WORKFLOW_MENTIONS;
	}

	function insertMention(wf: WorkflowOption) {
		if (chipCount() >= MAX_WORKFLOW_MENTIONS) {
			mentionOpen = false;
			mentionLocked = false;
			return;
		}
		const info = readAtQuery(editorEl);
		if (!editorEl) return;
		if (info) {
			const range = document.createRange();
			range.setStart(info.fromNode, info.fromOffset);
			range.setEnd(info.toNode, info.toOffset);
			range.deleteContents();
			const chip = makeChip(wf);
			range.insertNode(chip);
			const space = document.createTextNode('\u00a0');
			chip.after(space);
			placeCaretAfter(space);
		} else {
			editorEl.appendChild(makeChip(wf));
			const space = document.createTextNode('\u00a0');
			editorEl.appendChild(space);
			placeCaretAfter(space);
		}
		mentionOpen = false;
		mentionLocked = false;
		syncEmpty();
		editorEl.focus();
	}

	function makeChip(wf: WorkflowOption): HTMLSpanElement {
		const chip = document.createElement('span');
		chip.className = 'wf-chip';
		chip.contentEditable = 'false';
		chip.dataset.workflowId = String(wf.id);
		chip.dataset.workflowName = wf.name;
		chip.dataset.workflowKind = wf.kind;
		chip.textContent = `@${wf.name}`;
		return chip;
	}

	function insertSkill(skill: SkillOption) {
		if (!editorEl) return;
		const info = readTriggerQuery(editorEl, '/');
		if (info) {
			const range = document.createRange();
			range.setStart(info.fromNode, info.fromOffset);
			range.setEnd(info.toNode, info.toOffset);
			range.deleteContents();
			const chip = makeSkillChip(skill);
			range.insertNode(chip);
			const space = document.createTextNode('\u00a0');
			chip.after(space);
			placeCaretAfter(space);
		} else {
			editorEl.appendChild(makeSkillChip(skill));
			const space = document.createTextNode('\u00a0');
			editorEl.appendChild(space);
			placeCaretAfter(space);
		}
		slashOpen = false;
		syncEmpty();
		editorEl.focus();
	}

	function makeSkillChip(skill: SkillOption): HTMLSpanElement {
		const chip = document.createElement('span');
		chip.className = 'skill-chip';
		chip.contentEditable = 'false';
		chip.dataset.skillName = skill.name;
		chip.dataset.skillDescription = skill.description;
		chip.textContent = `/${skill.name}`;
		return chip;
	}

	async function addFiles(files: FileList | File[]) {
		const list = Array.from(files);
		for (const file of list) {
			if (attachments.length >= ATTACH_LIMIT) {
				toast.error(`At most ${ATTACH_LIMIT} attachments`);
				break;
			}
			const slot = `attach-${Date.now()}-${Math.random().toString(16).slice(2)}`;
			uploadingNames = [...uploadingNames, file.name];
			try {
				const path = await uploads.uploadSafe(slot, file);
				if (!path) continue;
				const meta = uploads.uploadMetaFor(path);
				attachments = [
					...attachments,
					{ path, name: meta.name || file.name, kind: meta.kind as AttachmentKind },
				];
			} finally {
				uploadingNames = uploadingNames.filter((n) => n !== file.name);
			}
		}
	}

	function removeAttachment(index: number) {
		attachments = attachments.filter((_, i) => i !== index);
	}

	function onFileChosen(e: Event) {
		const el = e.currentTarget as HTMLInputElement;
		// FileList is live — clearing the input empties it. Copy File objects first.
		const files = Array.from(el.files ?? []);
		el.value = '';
		if (files.length) void addFiles(files);
	}

	function onDrop(e: DragEvent) {
		e.preventDefault();
		dragOver = false;
		if (busy) return;
		const files = e.dataTransfer?.files;
		if (files && files.length) void addFiles(files);
	}

	function placeCaretAtEnd(el: HTMLElement) {
		const range = document.createRange();
		range.selectNodeContents(el);
		range.collapse(false);
		const sel = window.getSelection();
		sel?.removeAllRanges();
		sel?.addRange(range);
	}

	function placeCaretAfter(node: Node) {
		const range = document.createRange();
		range.setStartAfter(node);
		range.collapse(true);
		const sel = window.getSelection();
		sel?.removeAllRanges();
		sel?.addRange(range);
	}

	function caretRect(): DOMRect | null {
		const sel = window.getSelection();
		if (!sel || sel.rangeCount === 0) return null;
		const range = sel.getRangeAt(0).cloneRange();
		if (!range.collapsed) return range.getBoundingClientRect();
		const span = document.createElement('span');
		span.textContent = '\u200b';
		range.insertNode(span);
		const rect = span.getBoundingClientRect();
		span.parentNode?.removeChild(span);
		return rect;
	}

	/** Viewport box of the `@` caret, falling back to the editor when the caret has no size. */
	function caretAnchor(): { top: number; bottom: number; left: number } {
		const caret = caretRect();
		const box = editorEl?.getBoundingClientRect();
		if (caret && (caret.height > 0 || caret.width > 0)) {
			return {
				top: caret.top,
				bottom: caret.bottom,
				left: caret.left > 0 ? caret.left : (box?.left ?? 8),
			};
		}
		return {
			top: box?.top ?? 0,
			bottom: box?.bottom ?? 0,
			left: box?.left ?? 8,
		};
	}

	function readAtQuery(editor: HTMLElement | null) {
		return readTriggerQuery(editor, '@');
	}

	/** Detect `char` + unbroken query right before the caret. `skipSelector`
	 * excludes carets inside matching chips (e.g. inside an existing tag). */
	function readTriggerQuery(
		editor: HTMLElement | null,
		char: string,
		skipSelector?: string,
	): {
		query: string;
		fromNode: Node;
		fromOffset: number;
		toNode: Node;
		toOffset: number;
	} | null {
		if (!editor) return null;
		const sel = window.getSelection();
		if (!sel || !sel.isCollapsed || !sel.anchorNode || !editor.contains(sel.anchorNode)) {
			return null;
		}
		const node = sel.anchorNode;
		if (node.nodeType !== Node.TEXT_NODE) return null;
		if (skipSelector && (node.parentElement as HTMLElement | null)?.closest?.(skipSelector)) {
			return null;
		}
		const offset = sel.anchorOffset;
		const text = node.textContent ?? '';
		const before = text.slice(0, offset);
		const at = before.lastIndexOf(char);
		if (at < 0) return null;
		if (at > 0 && !/\s/.test(before[at - 1]!)) return null;
		const query = before.slice(at + 1);
		if (/[\s]/.test(query)) return null;
		return { query, fromNode: node, fromOffset: at, toNode: node, toOffset: offset };
	}
</script>

<div
	class="composer"
	class:drag-over={dragOver}
	role="region"
	aria-label="Message composer"
	ondragover={(e) => {
		e.preventDefault();
		dragOver = true;
	}}
	ondragleave={() => (dragOver = false)}
	ondrop={onDrop}
>
	{#if attachments.length > 0 || uploadingNames.length > 0}
		<div class="tray" aria-label="Attachments">
			{#each attachments as a, i (a.path)}
				<div class="tile">
					{#if a.kind === 'image'}
						{@const src = assetUrl(a.path)}
						{#if src}
							<img src={src} alt={a.name} />
						{/if}
					{:else}
						<span class="tile-icon">
							<Icon name={a.kind === 'audio' ? 'music' : 'video'} size={16} />
						</span>
					{/if}
					<span class="tile-name" title={a.name}>{truncateMiddle(a.name, 18)}</span>
					<button
						type="button"
						class="tile-x"
						title="Remove"
						onclick={() => removeAttachment(i)}
						disabled={running}
					>
						<Icon name="close" size={10} />
					</button>
				</div>
			{/each}
			{#each uploadingNames as name (name)}
				<div class="tile uploading">
					<span class="tile-name">{truncateMiddle(name, 18)}</span>
				</div>
			{/each}
		</div>
	{/if}

	<div class="box">
		<div
			bind:this={editorEl}
			class="editor"
			class:empty={editorEmpty}
			contenteditable={busy ? 'false' : 'true'}
			role="textbox"
			tabindex="0"
			aria-multiline="true"
			aria-label="Message"
			data-placeholder={running ? 'Agent is working…' : 'Describe what you want to build or change…'}
			onkeydown={onKeydown}
			oninput={onEditorInput}
			onpaste={onPaste}
		></div>
		<div class="actions">
			<input
				bind:this={fileInput}
				type="file"
				class="sr-only"
				accept={ACCEPT}
				multiple
				onchange={onFileChosen}
			/>
			<button
				type="button"
				class="attach"
				title="Attach image, video, or audio"
				disabled={busy}
				onclick={() => fileInput?.click()}
			>
				<Icon name="paperclip" size={16} />
			</button>
			{#if running}
				<button type="button" class="send stop" onclick={onCancel} title="Stop the agent">
					<Icon name="stop" size={14} />
					Stop
				</button>
			{:else}
				<button
					type="button"
					class="send"
					onclick={submit}
					disabled={!sendable}
					title="Send (Enter)"
				>
					Send
					<Icon name="chevron-right" size={14} />
				</button>
			{/if}
		</div>
	</div>
</div>

<WorkflowMentionMenu
	open={mentionOpen}
	items={mentionLocked ? [] : mentionItems}
	lockReason={mentionLocked
		? 'One workflow per message — remove the existing @ tag to pick another.'
		: null}
	activeIndex={mentionIndex}
	anchor={mentionAnchor}
	onSelect={insertMention}
	onHover={(i) => (mentionIndex = i)}
/>

<WorkflowMentionMenu
	open={slashOpen}
	items={slashItems}
	lockReason={null}
	activeIndex={slashIndex}
	anchor={slashAnchor}
	onSelect={insertSkill}
	onHover={(i) => (slashIndex = i)}
	skillMode
/>

<style>
	.composer {
		display: flex;
		flex-direction: column;
		gap: 8px;
		width: 100%;
		min-width: 0;
		padding-top: 10px;
		border-top: 1px solid var(--border);
	}
	.composer.drag-over .box {
		border-color: var(--accent);
		background: color-mix(in srgb, var(--accent) 8%, var(--bg-surface));
	}
	.tray {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}
	.tile {
		position: relative;
		display: flex;
		flex-direction: column;
		align-items: stretch;
		width: 72px;
		border-radius: var(--radius-sm);
		overflow: hidden;
		background: var(--bg-surface);
		border: 1px solid var(--border);
	}
	.tile img {
		display: block;
		width: 100%;
		height: 48px;
		object-fit: cover;
		background: var(--bg-elevated);
	}
	.tile-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		height: 48px;
		color: var(--text-muted);
		background: var(--bg-elevated);
	}
	.tile-name {
		padding: 3px 6px 4px;
		font-size: 10px;
		color: var(--text-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.tile-x {
		position: absolute;
		top: 3px;
		right: 3px;
		width: 16px;
		height: 16px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border: none;
		border-radius: 999px;
		background: rgba(0, 0, 0, 0.65);
		color: #fff;
		cursor: pointer;
		padding: 0;
	}
	.tile.uploading {
		opacity: 0.7;
		min-height: 64px;
		justify-content: center;
	}
	.box {
		display: flex;
		align-items: flex-end;
		gap: 8px;
		min-width: 0;
		background: var(--bg-surface);
		border: 1px solid var(--border);
		border-radius: var(--radius-md);
		padding: 8px 8px 8px 12px;
	}
	.editor {
		flex: 1;
		min-width: 0;
		min-height: 28px;
		max-height: 160px;
		overflow-y: auto;
		color: var(--text-primary);
		font-family: var(--font-body);
		font-size: 13.5px;
		line-height: 1.5;
		outline: none;
		white-space: pre-wrap;
		word-break: break-word;
	}
	.editor.empty::before {
		content: attr(data-placeholder);
		color: var(--text-muted);
		pointer-events: none;
	}
	.editor :global(.wf-chip) {
		display: inline;
		padding: 1px 7px;
		margin: 0 1px;
		border-radius: 999px;
		background: color-mix(in srgb, var(--accent) 22%, var(--bg-elevated));
		border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
		color: var(--accent);
		font-weight: 600;
		font-size: 13px;
		white-space: nowrap;
		user-select: none;
	}
	.editor :global(.skill-chip) {
		display: inline;
		padding: 1px 7px;
		margin: 0 1px;
		border-radius: 999px;
		background: color-mix(in srgb, #38bdf8 20%, var(--bg-elevated));
		border: 1px solid color-mix(in srgb, #38bdf8 45%, transparent);
		color: #7dd3fc;
		font-weight: 600;
		font-size: 13px;
		white-space: nowrap;
		user-select: none;
	}
	.actions {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-shrink: 0;
	}
	.attach {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		border: none;
		border-radius: var(--radius-md);
		background: transparent;
		color: var(--text-secondary);
		cursor: pointer;
	}
	.attach:hover:not(:disabled) {
		color: var(--text-primary);
		background: var(--bg-elevated);
	}
	.attach:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.attach:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 0;
	}
	.send {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 36px;
		padding: 0 14px;
		border-radius: var(--radius-md);
		border: 1px solid var(--accent);
		background: var(--accent);
		color: #fff;
		font-size: 13px;
		font-weight: 600;
		cursor: pointer;
		white-space: nowrap;
	}
	.send:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.send.stop {
		background: transparent;
		color: var(--error);
		border-color: rgba(239, 68, 68, 0.5);
	}
	.send:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
</style>
