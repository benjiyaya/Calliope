<script lang="ts">
	import { page } from '$app/stores';
	import { beforeNavigate, goto } from '$app/navigation';
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import SettingsNav from '$lib/components/settings/SettingsNav.svelte';
	import WorkflowsLibrary from '$lib/components/settings/WorkflowsLibrary.svelte';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import StatusChip from '$lib/components/ui/StatusChip.svelte';
	import { settings, type Settings } from '$lib/api';
	import { toast } from '$lib/toast';

	const client = useQueryClient();
	let tab = $derived(($page.url.searchParams.get('tab') || 'llm') as string);

	const settingsQuery = createQuery({
		queryKey: ['settings'],
		queryFn: settings.get,
	});

	let draft = $state<Record<string, unknown>>({});
	let apiKeyDraft = $state('');

	// Dirty tracking: any staged field (draft map or a typed API key) counts.
	const dirtyKeys = $derived([
		...Object.keys(draft).filter((k) => draft[k] !== undefined),
		...(apiKeyDraft ? ['llm_api_key'] : []),
	]);
	const isDirty = $derived(dirtyKeys.length > 0);

	const FIELD_TAB: Record<string, string> = {
		llm_base_url: 'llm',
		llm_model: 'llm',
		llm_api_key: 'llm',
		comfyui_base_url: 'comfy',
		dry_run: 'comfy',
		queue_concurrency: 'queue',
		queue_poll_interval_sec: 'queue',
		queue_max_retries: 'queue',
		data_dir: 'storage',
		assets_dir: 'storage',
		db_name: 'storage',
	};
	const dirtyTabs = $derived(
		new Set(dirtyKeys.map((k) => FIELD_TAB[k]).filter((t): t is string => Boolean(t))),
	);

	function discardDraft() {
		draft = {};
		apiKeyDraft = '';
	}

	const saveMutation = createMutation({
		mutationFn: () => {
			const update: Record<string, unknown> = {};
			for (const [k, v] of Object.entries(draft)) {
				if (v === undefined) continue;
				// Allow false for dry_run; skip empty optional strings only
				if (v === '' && k !== 'dry_run') continue;
				if (typeof v === 'string' && (k.includes('_dir') || k.endsWith('_dir'))) {
					// Strip wrapping quotes users often paste from Explorer
					const cleaned = v.trim().replace(/^["']|["']$/g, '');
					update[k] = cleaned;
					continue;
				}
				update[k] = v;
			}
			if (apiKeyDraft) update.llm_api_key = apiKeyDraft;
			return settings.update(update);
		},
		onSuccess: (saved) => {
			client.invalidateQueries({ queryKey: ['settings'] });
			discardDraft();
			toast.success(
				saved?.dry_run ? 'Settings saved (Dry-run is ON — placeholders only)' : 'Settings saved',
			);
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : 'Could not save settings');
		},
	});

	// Leave-guard: in-app navigation away from /settings asks to discard;
	// same-path tab switches keep the draft. Tab close uses beforeunload.
	let leaveOpen = $state(false);
	let pendingUrl = $state<string | null>(null);

	beforeNavigate((nav) => {
		if (!isDirty || !nav.to) return;
		if (nav.to.url.pathname === $page.url.pathname) return;
		nav.cancel();
		pendingUrl = `${nav.to.url.pathname}${nav.to.url.search}${nav.to.url.hash}`;
		leaveOpen = true;
	});

	function confirmLeave() {
		const url = pendingUrl;
		pendingUrl = null;
		discardDraft();
		if (url) goto(url);
	}

	function onBeforeUnload(e: BeforeUnloadEvent) {
		if (!isDirty) return;
		e.preventDefault();
		e.returnValue = '';
	}

	function fieldValue(key: keyof Settings, fallback: string | number | boolean | null | undefined) {
		if (draft[key] !== undefined && draft[key] !== '') return draft[key] as string;
		const raw = fallback ?? '';
		if (typeof raw === 'string') return raw.replace(/^["']|["']$/g, '');
		return raw;
	}

	function dryRunChecked(s: Settings): boolean {
		if (draft.dry_run !== undefined) return Boolean(draft.dry_run);
		return s.dry_run === true;
	}
</script>

<svelte:window onbeforeunload={onBeforeUnload} />

<div class="shell">
	<AppHeader active="settings" crumb="/ Settings">
		{#snippet status()}
			{#if isDirty}
				<StatusChip status="paused" label="Unsaved changes" />
			{/if}
		{/snippet}
	</AppHeader>

	<div class="body">
		<SettingsNav dirty={dirtyTabs} />
		<main class="content">
			{#if $settingsQuery.isLoading}
				<p class="muted">Loading settings…</p>
			{:else if $settingsQuery.data}
				{@const s = $settingsQuery.data}
				{#if tab === 'llm'}
					<section class="panel">
						<h1>LLM</h1>
						<p class="lead">OpenAI-compatible chat endpoint used for story and script drafting.</p>
						<label class="field">
							<span class="field-label">Base URL</span>
							<input
								class="field-input"
								value={String(fieldValue('llm_base_url', s.llm_base_url))}
								oninput={(e) => (draft.llm_base_url = e.currentTarget.value)}
								placeholder="http://127.0.0.1:11434/v1"
							/>
						</label>
						<label class="field">
							<span class="field-label">Model</span>
							<input
								class="field-input"
								value={String(fieldValue('llm_model', s.llm_model))}
								oninput={(e) => (draft.llm_model = e.currentTarget.value)}
								placeholder="llama3.2"
							/>
						</label>
						<label class="field">
							<span class="field-label">API key</span>
							<input
								class="field-input"
								type="password"
								bind:value={apiKeyDraft}
								placeholder={s.llm_api_key ? '•••••••• (saved)' : 'Optional for local servers'}
							/>
							<p class="field-hint">Stored in local config file, never in the project database.</p>
						</label>
					</section>
				{:else if tab === 'comfy'}
					<section class="panel">
						<h1>ComfyUI</h1>
						<p class="lead">Connection to your local render farm for image and video jobs.</p>
						<label class="field">
							<span class="field-label">Base URL</span>
							<input
								class="field-input"
								value={String(fieldValue('comfyui_base_url', s.comfyui_base_url))}
								oninput={(e) => (draft.comfyui_base_url = e.currentTarget.value)}
							/>
							<p class="field-hint">
								Calliope talks to Comfy over HTTP only. Comfy’s own input/output folders stay in
								ComfyUI — set them there, not here.
							</p>
						</label>
						<label class="check">
							<input
								type="checkbox"
								checked={dryRunChecked(s)}
								onchange={(e) => (draft.dry_run = e.currentTarget.checked)}
							/>
							Dry-run mode (off by default) — skip ComfyUI and write placeholder assets for testing only
						</label>
					</section>
				{:else if tab === 'queue'}
					<section class="panel">
						<h1>Queue</h1>
						<p class="lead">Worker concurrency and retry behavior for long GPU jobs.</p>
						<label class="field">
							<span class="field-label">Concurrency</span>
							<input
								class="field-input"
								type="number"
								min="1"
								max="8"
								value={String(fieldValue('queue_concurrency', s.queue_concurrency))}
								oninput={(e) => (draft.queue_concurrency = Number(e.currentTarget.value))}
							/>
						</label>
						<label class="field">
							<span class="field-label">Poll interval (seconds)</span>
							<input
								class="field-input"
								type="number"
								step="0.5"
								value={String(fieldValue('queue_poll_interval_sec', s.queue_poll_interval_sec))}
								oninput={(e) => (draft.queue_poll_interval_sec = Number(e.currentTarget.value))}
							/>
						</label>
						<label class="field">
							<span class="field-label">Max retries</span>
							<input
								class="field-input"
								type="number"
								value={String(fieldValue('queue_max_retries', s.queue_max_retries))}
								oninput={(e) => (draft.queue_max_retries = Number(e.currentTarget.value))}
							/>
						</label>
					</section>
				{:else if tab === 'storage'}
					<section class="panel">
						<h1>Storage</h1>
						<p class="lead">Where Calliope keeps SQLite and generated assets on disk.</p>
						<div class="callout">
							<Icon name="alert" size={16} />
							<p>
								<strong>Changing storage paths moves where Calliope writes data.</strong>
								Do not point this at temporary folders — they are wiped.
							</p>
						</div>
						<label class="field">
							<span class="field-label">Data directory</span>
							<input
								class="field-input"
								value={String(fieldValue('data_dir', s.data_dir))}
								oninput={(e) => (draft.data_dir = e.currentTarget.value)}
							/>
							<p class="field-hint">Current: <code class="mono">{s.data_dir}</code></p>
						</label>
						<label class="field">
							<span class="field-label">Assets directory</span>
							<input
								class="field-input"
								value={String(fieldValue('assets_dir', s.assets_dir))}
								oninput={(e) => (draft.assets_dir = e.currentTarget.value)}
							/>
							<p class="field-hint">Current: <code class="mono">{s.assets_dir}</code></p>
						</label>
					</section>
				{:else if tab === 'workflows'}
					<WorkflowsLibrary />
				{/if}

				{#if tab !== 'workflows'}
					<div class="save-bar">
						<span class="save-state" class:dirty={isDirty}>
							{#if isDirty}
								<span class="save-dot" aria-hidden="true"></span>Unsaved changes
							{:else}
								All changes saved
							{/if}
						</span>
						<Button
							variant="ghost"
							disabled={!isDirty || $saveMutation.isPending}
							onclick={discardDraft}
						>
							Discard
						</Button>
						<Button
							variant="primary"
							disabled={!isDirty}
							loading={$saveMutation.isPending}
							onclick={() => $saveMutation.mutate()}
						>
							Save changes
						</Button>
					</div>
				{/if}
			{/if}
		</main>
	</div>
</div>

<ConfirmDialog
	bind:open={leaveOpen}
	title="Discard unsaved changes?"
	message="You have unsaved settings changes. Leaving now will discard them."
	confirmLabel="Discard and leave"
	danger
	onconfirm={confirmLeave}
	oncancel={() => (pendingUrl = null)}
/>

<style>
	.shell {
		min-height: 100vh;
		display: flex;
		flex-direction: column;
	}
	.body {
		display: flex;
		flex: 1;
		min-height: 0;
	}
	.content {
		flex: 1;
		padding: var(--space-xl);
		overflow-y: auto;
		max-width: 960px;
	}
	.panel {
		background: rgba(255, 255, 255, 0.03);
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-radius: var(--radius-lg);
		padding: var(--space-lg);
	}
	.panel h1 {
		margin: 0 0 6px;
		font-size: 22px;
	}
	.lead {
		margin: 0 0 var(--space-lg);
		color: var(--text-secondary);
		font-size: 14px;
	}
	.check {
		display: flex;
		align-items: center;
		gap: 10px;
		font-size: 14px;
		color: var(--text-secondary);
	}
	.check input {
		width: auto;
	}
	.callout {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		padding: 12px 14px;
		margin-bottom: var(--space-lg);
		border-radius: var(--radius-md);
		border: 1px solid rgba(245, 158, 11, 0.35);
		background: rgba(245, 158, 11, 0.08);
		color: var(--warning);
	}
	.callout :global(.icon) {
		flex-shrink: 0;
		margin-top: 2px;
	}
	.callout p {
		margin: 0;
		font-size: 13px;
		line-height: 1.5;
		color: var(--text-secondary);
	}
	.callout strong {
		color: var(--warning);
	}
	.muted {
		color: var(--text-muted);
	}
	.save-bar {
		position: sticky;
		bottom: 0;
		z-index: 5;
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: var(--space-sm);
		margin: var(--space-xl) calc(-1 * var(--space-xl)) calc(-1 * var(--space-xl));
		padding: var(--space-md) var(--space-xl);
		background: var(--bg-surface);
		border-top: 1px solid var(--border);
		box-shadow: 0 -8px 24px rgba(0, 0, 0, 0.25);
	}
	.save-state {
		margin-right: auto;
		display: inline-flex;
		align-items: center;
		gap: 8px;
		font-size: 12px;
		color: var(--text-muted);
	}
	.save-state.dirty {
		color: var(--warning);
	}
	.save-dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--warning);
	}
</style>
