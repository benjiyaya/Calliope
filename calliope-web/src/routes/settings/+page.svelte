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
	import { settings, type LlmProfile, type Settings } from '$lib/api';
	import { toast } from '$lib/toast';

	const client = useQueryClient();
	let tab = $derived(($page.url.searchParams.get('tab') || 'llm') as string);

	const settingsQuery = createQuery({
		queryKey: ['settings'],
		queryFn: settings.get,
	});

	let draft = $state<Record<string, unknown>>({});
	let apiKeyDrafts = $state<Record<string, string>>({});

	// Dirty tracking: any staged field (draft map or a typed API key) counts.
	const dirtyKeys = $derived([
		...Object.keys(draft).filter((k) => draft[k] !== undefined),
		...(Object.values(apiKeyDrafts).some((v) => v) ? ['llm_api_key'] : []),
	]);
	const isDirty = $derived(dirtyKeys.length > 0);

	const FIELD_TAB: Record<string, string> = {
		llm_profiles: 'llm',
		llm_active_id: 'llm',
		llm_api_key: 'llm',
		comfyui_base_url: 'comfy',
		dry_run: 'comfy',
		queue_concurrency: 'queue',
		queue_poll_interval_sec: 'queue',
		queue_poll_timeout_sec: 'queue',
		queue_max_retries: 'queue',
		agent_max_steps: 'queue',
		agent_hardening_prompt: 'agent',
		agent_llm_assignments: 'agent',
		data_dir: 'storage',
		assets_dir: 'storage',
		db_name: 'storage',
	};
	// Mirrors backend Field(ge=..., le=...) limits — validated client-side so
	// save never trips a raw 422.
	const NUMERIC_LIMITS: Record<string, { min: number; max: number; label: string }> = {
		queue_concurrency: { min: 1, max: 8, label: 'Concurrency' },
		queue_poll_interval_sec: { min: 0.5, max: 60, label: 'Poll interval' },
		queue_poll_timeout_sec: { min: 0, max: 86400, label: 'Poll timeout' },
		queue_max_retries: { min: 0, max: 10, label: 'Max retries' },
		agent_max_steps: { min: 1, max: 100, label: 'Agent max steps' },
	};
	const dirtyTabs = $derived(
		new Set(dirtyKeys.map((k) => FIELD_TAB[k]).filter((t): t is string => Boolean(t))),
	);

	// Validation for numeric fields — mirrors backend Field(ge/le) limits.
	function fieldError(key: string): string | null {
		const limits = NUMERIC_LIMITS[key];
		if (!limits) return null;
		const raw = draft[key];
		if (raw === undefined || raw === '' || raw === null) return null;
		const v = Number(raw);
		if (Number.isNaN(v)) return `${limits.label} must be a number`;
		if (v < limits.min) return `${limits.label} must be at least ${limits.min}`;
		if (v > limits.max) return `${limits.label} must be at most ${limits.max}`;
		return null;
	}

	const validationErrors = $derived.by(() => {
		const errors: Record<string, string> = {};
		for (const key of Object.keys(NUMERIC_LIMITS)) {
			const err = fieldError(key);
			if (err) errors[key] = err;
		}
		const profiles = Array.isArray(draft.llm_profiles) ? (draft.llm_profiles as LlmProfile[]) : null;
		if (profiles) {
			if (profiles.length === 0) errors.llm_profiles = 'Add at least one LLM';
			for (const p of profiles) {
				if (!p.name.trim()) errors[`llm_name_${p.id}`] = 'Name is required';
				if (!p.base_url.trim()) errors[`llm_url_${p.id}`] = 'Base URL is required';
				if (!p.model.trim()) errors[`llm_model_${p.id}`] = 'Model is required';
			}
		}
		return errors;
	});
	const isValid = $derived(Object.keys(validationErrors).length === 0);

	function discardDraft() {
		draft = {};
		apiKeyDrafts = {};
	}

	const saveMutation = createMutation({
		mutationFn: () => {
			const update: Record<string, unknown> = {};
			for (const [k, v] of Object.entries(draft)) {
				if (v === undefined) continue;
				if (k === 'llm_profiles' || k === 'llm_active_id') continue;
				// Allow false for dry_run; skip empty optional strings only.
				// agent_hardening_prompt may be emptied to disable hardening.
				if (v === '' && k !== 'dry_run' && k !== 'agent_hardening_prompt') continue;
				if (typeof v === 'string' && (k.includes('_dir') || k.endsWith('_dir'))) {
					// Strip wrapping quotes users often paste from Explorer
					const cleaned = v.trim().replace(/^["']|["']$/g, '');
					update[k] = cleaned;
					continue;
				}
				update[k] = v;
			}
			if (Array.isArray(draft.llm_profiles)) {
				update.llm_profiles = (draft.llm_profiles as LlmProfile[]).map((p) => {
					const row: Record<string, unknown> = {
						id: p.id,
						name: p.name.trim(),
						base_url: p.base_url.trim(),
						model: p.model.trim(),
					};
					const key = apiKeyDrafts[p.id];
					if (key) row.api_key = key;
					return row;
				});
			}
			if (typeof draft.llm_active_id === 'string' && draft.llm_active_id) {
				update.llm_active_id = draft.llm_active_id;
			}
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
			// Surface pydantic 422 validation dumps as a readable message.
			let msg = err instanceof Error ? err.message : 'Could not save settings';
			const m = msg.match(/"msg":"([^"]+)"/);
			if (m) {
				const field = msg.match(/\["body","([a-z_]+)"\]/);
				msg = field
					? `Invalid ${field[1].replace(/_/g, ' ')}: ${m[1]}`
					: `Invalid value: ${m[1]}`;
			}
			toast.error(msg);
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

	// Snap out-of-range numbers back into the valid range when the user leaves
	// the field, so a typed 200 never reaches the backend.
	function clampOnBlur(e: FocusEvent, key: string) {
		const limits = NUMERIC_LIMITS[key];
		if (!limits) return;
		const el = e.currentTarget as HTMLInputElement;
		const v = Number(el.value);
		if (el.value === '' || Number.isNaN(v)) return;
		const clamped = Math.min(Math.max(v, limits.min), limits.max);
		if (clamped !== v) {
			el.value = String(clamped);
			draft[key] = clamped;
		}
	}

	// The hardening prompt may be intentionally emptied, so it can't reuse
	// fieldValue (which falls back to the saved value on '').
	function hardeningDraft(s: Settings): string {
		if (draft.agent_hardening_prompt !== undefined) {
			return String(draft.agent_hardening_prompt);
		}
		return s.agent_hardening_prompt ?? '';
	}

	function llmProfilesFromSettings(s: Settings): LlmProfile[] {
		if (s.llm_profiles?.length) return s.llm_profiles.map((p) => ({ ...p }));
		return [
			{
				id: s.llm_active_id || 'legacy',
				name: s.llm_model || 'Default',
				base_url: s.llm_base_url,
				model: s.llm_model,
				api_key: s.llm_api_key,
			},
		];
	}

	function workingProfiles(s: Settings): LlmProfile[] {
		if (Array.isArray(draft.llm_profiles)) return draft.llm_profiles as LlmProfile[];
		return llmProfilesFromSettings(s);
	}

	function workingActiveId(s: Settings): string {
		if (typeof draft.llm_active_id === 'string' && draft.llm_active_id) {
			return draft.llm_active_id;
		}
		return s.llm_active_id || workingProfiles(s)[0]?.id || '';
	}

	const AGENT_ROLES: { key: string; label: string; hint: string }[] = [
		{ key: 'main', label: 'Main agent', hint: 'The chat loop that answers you and calls tools' },
		{ key: 'planner', label: 'Planner', hint: 'Decides single vs swarm; writes the final swarm summary' },
		{ key: 'story', label: 'Story agent', hint: 'Sub-agent for story beats' },
		{ key: 'script', label: 'Script agent', hint: 'Sub-agent for scenes and script text' },
		{ key: 'assets', label: 'Assets agent', hint: 'Sub-agent for characters, locations, items' },
		{ key: 'video', label: 'Video agent', hint: 'Sub-agent for clip generation; also the H3 prompt rewrite' },
	];

	function assignmentValue(s: Settings, key: string): string {
		if (draft.agent_llm_assignments !== undefined) {
			const map = draft.agent_llm_assignments as Record<string, string | null>;
			return map[key] ?? '';
		}
		return s.agent_llm_assignments?.[key] ?? '';
	}

	function setAssignment(key: string, value: string) {
		const current = draft.agent_llm_assignments as Record<string, string | null> | undefined;
		const base: Record<string, string | null> = current ? { ...current } : {};
		base[key] = value === '' ? null : value;
		draft.agent_llm_assignments = base;
	}

	function ensureLlmDraft(s: Settings) {
		if (!Array.isArray(draft.llm_profiles)) {
			draft.llm_profiles = llmProfilesFromSettings(s);
		}
		if (typeof draft.llm_active_id !== 'string' || !draft.llm_active_id) {
			draft.llm_active_id = s.llm_active_id || (draft.llm_profiles as LlmProfile[])[0]?.id;
		}
	}

	function patchProfile(s: Settings, id: string, patch: Partial<LlmProfile>) {
		ensureLlmDraft(s);
		draft.llm_profiles = (draft.llm_profiles as LlmProfile[]).map((p) =>
			p.id === id ? { ...p, ...patch } : p,
		);
	}

	function setActiveProfile(s: Settings, id: string) {
		ensureLlmDraft(s);
		draft.llm_active_id = id;
	}

	function addProfile(s: Settings) {
		ensureLlmDraft(s);
		const id = crypto.randomUUID();
		draft.llm_profiles = [
			...(draft.llm_profiles as LlmProfile[]),
			{
				id,
				name: 'New LLM',
				base_url: 'http://127.0.0.1:11434/v1',
				model: '',
				api_key: false,
			},
		];
	}

	function removeProfile(s: Settings, id: string) {
		ensureLlmDraft(s);
		const list = (draft.llm_profiles as LlmProfile[]).filter((p) => p.id !== id);
		if (list.length === 0) return;
		draft.llm_profiles = list;
		if (workingActiveId(s) === id) {
			draft.llm_active_id = list[0].id;
		}
		const nextKeys = { ...apiKeyDrafts };
		delete nextKeys[id];
		apiKeyDrafts = nextKeys;
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
						<div class="panel-head">
							<div>
								<h1>LLM</h1>
								<p class="lead">
									OpenAI-compatible chat endpoints used for story, script, and the agent.
									Save several, then choose which one Calliope should use.
								</p>
							</div>
							<Button variant="secondary" size="sm" onclick={() => addProfile(s)}>
								<Icon name="plus" size={14} />
								Add LLM
							</Button>
						</div>
						{#if validationErrors.llm_profiles}
							<p class="field-error">{validationErrors.llm_profiles}</p>
						{/if}
						<div class="llm-list">
							{#each workingProfiles(s) as profile (profile.id)}
								{@const active = workingActiveId(s) === profile.id}
								<article class="llm-card" class:active>
									<header class="llm-card-head">
										<label class="llm-active">
											<input
												type="radio"
												name="llm-active"
												checked={active}
												onchange={() => setActiveProfile(s, profile.id)}
											/>
											<span>{active ? 'Active' : 'Use this'}</span>
										</label>
										{#if workingProfiles(s).length > 1}
											<Button
												variant="ghost"
												size="sm"
												title="Remove this LLM"
												onclick={() => removeProfile(s, profile.id)}
											>
												<Icon name="trash" size={14} />
												Remove
											</Button>
										{/if}
									</header>
									<label class="field">
										<span class="field-label">Name</span>
										<input
											class="field-input"
											class:invalid={validationErrors[`llm_name_${profile.id}`]}
											value={profile.name}
											oninput={(e) => patchProfile(s, profile.id, { name: e.currentTarget.value })}
											placeholder="Local Ollama"
										/>
										{#if validationErrors[`llm_name_${profile.id}`]}
											<p class="field-error">{validationErrors[`llm_name_${profile.id}`]}</p>
										{/if}
									</label>
									<label class="field">
										<span class="field-label">Base URL</span>
										<input
											class="field-input"
											class:invalid={validationErrors[`llm_url_${profile.id}`]}
											value={profile.base_url}
											oninput={(e) =>
												patchProfile(s, profile.id, { base_url: e.currentTarget.value })}
											placeholder="http://127.0.0.1:11434/v1"
										/>
										{#if validationErrors[`llm_url_${profile.id}`]}
											<p class="field-error">{validationErrors[`llm_url_${profile.id}`]}</p>
										{/if}
									</label>
									<label class="field">
										<span class="field-label">Model</span>
										<input
											class="field-input"
											class:invalid={validationErrors[`llm_model_${profile.id}`]}
											value={profile.model}
											oninput={(e) => patchProfile(s, profile.id, { model: e.currentTarget.value })}
											placeholder="llama3.2"
										/>
										{#if validationErrors[`llm_model_${profile.id}`]}
											<p class="field-error">{validationErrors[`llm_model_${profile.id}`]}</p>
										{/if}
									</label>
									<label class="field">
										<span class="field-label">API key</span>
										<input
											class="field-input"
											type="password"
											value={apiKeyDrafts[profile.id] ?? ''}
											oninput={(e) => {
												ensureLlmDraft(s);
												apiKeyDrafts = {
													...apiKeyDrafts,
													[profile.id]: e.currentTarget.value,
												};
											}}
											placeholder={profile.api_key
												? '•••••••• (saved)'
												: 'Optional for local servers'}
										/>
										<p class="field-hint">
											Stored in local config file, never in the project database.
										</p>
									</label>
								</article>
							{/each}
						</div>
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
							class:invalid={validationErrors.queue_concurrency}
							type="number"
							min="1"
							max="8"
							step="1"
							value={String(fieldValue('queue_concurrency', s.queue_concurrency))}
							oninput={(e) => (draft.queue_concurrency = e.currentTarget.value)}
							onblur={(e) => clampOnBlur(e, 'queue_concurrency')}
						/>
						{#if validationErrors.queue_concurrency}
							<p class="field-error">{validationErrors.queue_concurrency}</p>
						{/if}
					</label>
					<label class="field">
						<span class="field-label">Poll interval (seconds)</span>
						<input
							class="field-input"
							class:invalid={validationErrors.queue_poll_interval_sec}
							type="number"
							min="0.5"
							max="60"
							step="0.5"
							value={String(fieldValue('queue_poll_interval_sec', s.queue_poll_interval_sec))}
							oninput={(e) => (draft.queue_poll_interval_sec = e.currentTarget.value)}
							onblur={(e) => clampOnBlur(e, 'queue_poll_interval_sec')}
						/>
						{#if validationErrors.queue_poll_interval_sec}
							<p class="field-error">{validationErrors.queue_poll_interval_sec}</p>
						{/if}
					</label>
					<label class="field">
						<span class="field-label">Poll timeout (seconds, 0 = no limit)</span>
						<input
							class="field-input"
							class:invalid={validationErrors.queue_poll_timeout_sec}
							type="number"
							min="0"
							max="86400"
							step="1"
							value={String(fieldValue('queue_poll_timeout_sec', s.queue_poll_timeout_sec))}
							oninput={(e) => (draft.queue_poll_timeout_sec = e.currentTarget.value)}
							onblur={(e) => clampOnBlur(e, 'queue_poll_timeout_sec')}
						/>
						{#if validationErrors.queue_poll_timeout_sec}
							<p class="field-error">{validationErrors.queue_poll_timeout_sec}</p>
						{/if}
						<p class="field-hint">
							How long the worker waits on ComfyUI for a job before failing it. Long video
							generations can exceed 10 minutes — raise this, or set 0 to wait indefinitely.
						</p>
					</label>
					<label class="field">
						<span class="field-label">Max retries</span>
						<input
							class="field-input"
							class:invalid={validationErrors.queue_max_retries}
							type="number"
							min="0"
							max="10"
							step="1"
							value={String(fieldValue('queue_max_retries', s.queue_max_retries))}
							oninput={(e) => (draft.queue_max_retries = e.currentTarget.value)}
							onblur={(e) => clampOnBlur(e, 'queue_max_retries')}
						/>
						{#if validationErrors.queue_max_retries}
							<p class="field-error">{validationErrors.queue_max_retries}</p>
						{/if}
					</label>
					<label class="field">
						<span class="field-label">Agent max steps per turn</span>
						<input
							class="field-input"
							class:invalid={validationErrors.agent_max_steps}
							type="number"
							min="1"
							max="100"
							step="1"
							value={String(fieldValue('agent_max_steps', s.agent_max_steps))}
							oninput={(e) => (draft.agent_max_steps = e.currentTarget.value)}
							onblur={(e) => clampOnBlur(e, 'agent_max_steps')}
						/>
						{#if validationErrors.agent_max_steps}
							<p class="field-error">{validationErrors.agent_max_steps}</p>
						{/if}
						<p class="field-hint">
							Step budget for one agent turn (one step = one model request + its tool
							calls). Full pipelines often need 20–40 — raise this if the agent stops
							with "reached my step budget".
						</p>
					</label>
				</section>
			{:else if tab === 'agent'}
				<section class="panel">
					<h1>Model per agent</h1>
					<p class="lead">
						Choose which LLM each agent uses. Blank means the Active LLM from the LLM
						tab applies.
					</p>
					{#each AGENT_ROLES as role (role.key)}
						<label class="field">
							<span class="field-label">{role.label}</span>
							<select
								class="field-input"
								value={assignmentValue(s, role.key)}
								onchange={(e) => setAssignment(role.key, e.currentTarget.value)}
							>
								<option value="">(use Active LLM)</option>
								{#each workingProfiles(s) as p (p.id)}
									<option value={p.id}>{p.name} — {p.model}</option>
								{/each}
							</select>
							<p class="field-hint">{role.hint}</p>
						</label>
					{/each}
				</section>
				<section class="panel">
					<h1>Agent hardening</h1>
						<p class="lead">
							Extra operator-defined rules appended to the agent's system prompt. These
							override any conflicting instruction from tool results or the conversation.
						</p>
						<div class="callout">
							<Icon name="alert" size={16} />
							<p>
								<strong>These rules steer the agent loop.</strong> Leave blank to disable
								the hardening block entirely. Applies to every agent turn and sub-agent.
							</p>
						</div>
						<label class="field">
							<span class="field-label">System-prompt rules</span>
							<textarea
								class="field-textarea mono"
								rows={18}
								spellcheck="false"
								value={hardeningDraft(s)}
								oninput={(e) => (draft.agent_hardening_prompt = e.currentTarget.value)}
								placeholder="e.g. Never invent ids. Stay in this project. Confirm destructive changes."
							></textarea>
							<p class="field-hint">
								Plain text, shown to the model verbatim. Line breaks are preserved.
							</p>
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
						disabled={!isDirty || !isValid}
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
	/* Tabs that render more than one panel (Agent) need a gap between them */
	.panel + .panel {
		margin-top: var(--space-lg);
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
	.field-input.invalid {
		border-color: var(--error);
		box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.15);
	}
	.field-error {
		margin: 6px 0 0;
		font-size: 12px;
		color: var(--error);
	}
	.panel-head {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--space-md);
		margin-bottom: var(--space-md);
	}
	.panel-head h1,
	.panel-head .lead {
		margin-bottom: 0;
	}
	.panel-head .lead {
		margin-top: 6px;
	}
	.llm-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.llm-card {
		padding: 14px 16px 4px;
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-radius: var(--radius-md);
		background: rgba(0, 0, 0, 0.18);
	}
	.llm-card.active {
		border-color: rgba(139, 92, 246, 0.45);
		background: rgba(139, 92, 246, 0.08);
	}
	.llm-card-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin-bottom: var(--space-sm);
	}
	.llm-active {
		display: inline-flex;
		align-items: center;
		gap: 8px;
		font-size: 13px;
		font-weight: 600;
		color: var(--text-secondary);
		cursor: pointer;
	}
	.llm-card.active .llm-active {
		color: var(--accent);
	}
	.llm-active input {
		width: auto;
		accent-color: var(--accent);
	}
</style>
