<script lang="ts">
	import { beforeNavigate, goto, afterNavigate } from '$app/navigation';
	import { createMutation, createQuery, useQueryClient } from '@tanstack/svelte-query';
	import SettingsNav from '$lib/components/settings/SettingsNav.svelte';
	import WorkflowsLibrary from '$lib/components/settings/WorkflowsLibrary.svelte';
	import SkillsLibrary from '$lib/components/settings/SkillsLibrary.svelte';
	import MemoryPanel from '$lib/components/settings/MemoryPanel.svelte';
	import AppHeader from '$lib/components/AppHeader.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';
	import StatusChip from '$lib/components/ui/StatusChip.svelte';
	import {
		settings,
		type LlmModelInfo,
		type LlmModelsResponse,
		type LlmProfile,
		type LlmTestResponse,
		type Settings,
		type ThinkingMode,
	} from '$lib/api';
	import { toast } from '$lib/toast';
	import { t } from '$lib/i18n.svelte';

	const client = useQueryClient();
	// Tab is tracked imperatively via afterNavigate (below) rather than a
	// `page`-derived value: query-only same-route navigation (?tab=llm →
	// ?tab=skills) did not retrigger the derived in every bundle, leaving
	// the previous tab's content on screen while the URL changed. An event
	// handler fires deterministically on every completed navigation.
	let tab = $state('llm');

	afterNavigate((nav) => {
		tab = (nav.to?.url.searchParams.get('tab') || 'llm') as string;
	});

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
		agent_history_char_budget: 'agent',
		agent_history_token_share: 'agent',
		llm_max_output_tokens: 'agent',
		llm_chars_per_token: 'agent',
		llm_context_fallback_tokens: 'agent',
		llm_context_tokens: 'agent',
		data_dir: 'storage',
		assets_dir: 'storage',
		agent_workspace_dir: 'storage',
		db_name: 'storage',
		agent_shell_enabled: 'agent',
	};
	// Mirrors backend Field(ge=..., le=...) limits — validated client-side so
	// save never trips a raw 422.
	const NUMERIC_LIMITS: Record<string, { min: number; max: number; labelKey: string }> = {
		queue_concurrency: { min: 1, max: 8, labelKey: 'settings.concurrencyField' },
		queue_poll_interval_sec: { min: 0.5, max: 60, labelKey: 'settings.pollIntervalField' },
		queue_poll_timeout_sec: { min: 0, max: 86400, labelKey: 'settings.pollTimeoutField' },
		queue_max_retries: { min: 0, max: 10, labelKey: 'settings.maxRetriesField' },
		agent_max_steps: { min: 1, max: 100, labelKey: 'settings.agentMaxStepsField' },
		agent_history_char_budget: { min: 0, max: 2000000, labelKey: 'settings.historyBudgetField' },
		agent_history_token_share: { min: 0.05, max: 0.95, labelKey: 'settings.historyShareField' },
		llm_max_output_tokens: { min: 0, max: 200000, labelKey: 'settings.maxOutputField' },
		llm_chars_per_token: { min: 0.5, max: 8, labelKey: 'settings.charsPerTokenField' },
		llm_context_fallback_tokens: { min: 1024, max: 10000000, labelKey: 'settings.fallbackCtxField' },
		llm_context_tokens: { min: 0, max: 10000000, labelKey: 'settings.contextOverrideField' },
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
		if (Number.isNaN(v)) return t('settings.notNumber', { label: t(limits.labelKey) });
		if (v < limits.min) return t('settings.minError', { label: t(limits.labelKey), min: limits.min });
		if (v > limits.max) return t('settings.maxError', { label: t(limits.labelKey), max: limits.max });
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
			if (profiles.length === 0) errors.llm_profiles = t('settings.addAtLeastOneLlm');
			for (const p of profiles) {
				if (!p.name.trim()) errors[`llm_name_${p.id}`] = t('settings.requiredName');
				if (!p.base_url.trim()) errors[`llm_url_${p.id}`] = t('settings.requiredBaseUrl');
				if (!p.model.trim()) errors[`llm_model_${p.id}`] = t('settings.requiredModel');
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
						thinking: p.thinking ?? 'default',
						// Sent back so the backend's "absent means keep" rule does not
						// discard the window the probe cached.
						context_tokens: p.context_tokens ?? 0,
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
				saved?.dry_run ? t('settings.savedDryRun') : t('settings.saved'),
			);
		},
		onError: (err) => {
			// Surface pydantic 422 validation dumps as a readable message.
			let msg = err instanceof Error ? err.message : t('settings.saveFailed');
			const m = msg.match(/"msg":"([^"]+)"/);
			if (m) {
				const field = msg.match(/\["body","([a-z_]+)"\]/);
				msg = field
					? t('settings.invalidField', {
							field: field[1].replace(/_/g, ' '),
							detail: m[1],
						})
					: t('settings.invalidValue', { detail: m[1] });
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
		if (nav.to.url.pathname === nav.from?.url.pathname) return;
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

	function shellChecked(s: Settings): boolean {
		if (draft.agent_shell_enabled !== undefined) return Boolean(draft.agent_shell_enabled);
		return s.agent_shell_enabled === true;
	}

	// History budget presets (chars). Large matches the backend default (400k).
	const HISTORY_BUDGET_PRESETS: { value: number; labelKey: string }[] = [
		{ value: 0, labelKey: 'settings.historyBudgetAuto' },
		{ value: 60_000, labelKey: 'settings.historyBudgetSmall' },
		{ value: 120_000, labelKey: 'settings.historyBudgetMedium' },
		{ value: 400_000, labelKey: 'settings.historyBudgetLarge' },
	];

	function historyBudgetActive(s: Settings, value: number): boolean {
		const current =
			draft.agent_history_char_budget !== undefined
				? Number(draft.agent_history_char_budget)
				: Number(s.agent_history_char_budget ?? 0);
		return current === value;
	}

	// What the backend will actually trim to, so the operator sees the effect
	// of their edit instead of having to save and reopen. The backend computes
	// the real number (it owns the profile's probed context window); this is the
	// same arithmetic for the unsaved draft.
	//
	// `$settingsQuery.data`, not the template's `{@const s}`: that binding only
	// exists inside the markup block, not in script scope.
	const savedCharBudget = $derived(
		Number($settingsQuery.data?.agent_history_char_budget ?? 0),
	);
	const effectiveBudgetChars = $derived.by(() => {
		const override = Number(
			draft.agent_history_char_budget !== undefined
				? draft.agent_history_char_budget
				: savedCharBudget,
		);
		if (Number.isFinite(override) && override > 0) return override;
		const share = Number(
			draft.agent_history_token_share !== undefined
				? draft.agent_history_token_share
				: ($settingsQuery.data?.agent_history_token_share ?? 0.5),
		);
		const perToken = Number(
			draft.llm_chars_per_token !== undefined
				? draft.llm_chars_per_token
				: ($settingsQuery.data?.llm_chars_per_token ?? 1.6),
		);
		const ctx = Number($settingsQuery.data?.context_window_tokens ?? 0);
		if (!ctx) return 0;
		const clampedShare = Math.min(Math.max(share || 0.5, 0.05), 0.95);
		const clampedPer = Math.min(Math.max(perToken || 1.6, 0.5), 8);
		return Math.floor(ctx * clampedShare * clampedPer);
	});
	const effectiveBudgetTokens = $derived(
		Math.round(
			effectiveBudgetChars /
				(Number($settingsQuery.data?.llm_chars_per_token ?? 1.6) || 1.6),
		),
	);

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
		if (s.llm_profiles?.length) {
			return s.llm_profiles.map((p) => ({ ...p, thinking: p.thinking ?? 'default' }));
		}
		return [
			{
				id: s.llm_active_id || 'legacy',
				name: s.llm_model || t('settings.llmDefaultName'),
				base_url: s.llm_base_url,
				model: s.llm_model,
				api_key: s.llm_api_key,
				thinking: 'default',
				context_tokens: 0,
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

	const AGENT_ROLES: { key: string; labelKey: string; hintKey: string }[] = [
		{ key: 'main', labelKey: 'settings.roleMain', hintKey: 'settings.roleMainHint' },
		{ key: 'planner', labelKey: 'settings.rolePlanner', hintKey: 'settings.rolePlannerHint' },
		{ key: 'story', labelKey: 'settings.roleStory', hintKey: 'settings.roleStoryHint' },
		{ key: 'script', labelKey: 'settings.roleScript', hintKey: 'settings.roleScriptHint' },
		{ key: 'assets', labelKey: 'settings.roleAssets', hintKey: 'settings.roleAssetsHint' },
		{ key: 'video', labelKey: 'settings.roleVideo', hintKey: 'settings.roleVideoHint' },
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
				name: t('settings.newLlmDefault'),
				base_url: 'http://127.0.0.1:11434/v1',
				model: '',
				api_key: false,
				thinking: 'default',
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
		const nextModels = { ...modelLists };
		delete nextModels[id];
		modelLists = nextModels;
		const nextProbes = { ...probeResults };
		delete nextProbes[id];
		probeResults = nextProbes;
	}

	// ---- Endpoint introspection -------------------------------------------------
	// Per-profile, keyed by profile id, and deliberately NOT part of `draft`: a
	// probe result is read-only server state, and folding it into the dirty map
	// would make the leave-guard nag about unsaved changes the user never made.
	let modelLists = $state<Record<string, LlmModelsResponse>>({});
	let probing = $state<Record<string, boolean>>({});
	let probeResults = $state<Record<string, LlmTestResponse | null>>({});

	const THINKING_MODES: ThinkingMode[] = ['default', 'off', 'low', 'medium', 'high', 'xhigh'];

	type ModelOption = { id: string; label: string; info: LlmModelInfo | null };

	function modelOptions(p: LlmProfile): ModelOption[] {
		const listed: ModelOption[] = (modelLists[p.id]?.models ?? []).map((m) => {
			const bits: string[] = [];
			if (m.vision) bits.push(t('settings.probeVision'));
			if (m.ctx) bits.push(`${(m.ctx / 1024) | 0}K`);
			if (m.reasoning_disabled) bits.push(t('settings.probeNoThinking'));
			else if (m.reasoning_effort) bits.push(`${t('settings.probeThink')}:${m.reasoning_effort}`);
			if (m.loaded === 'loaded') bits.push(t('settings.probeLoaded'));
			return { id: m.id, label: bits.length ? `${m.id}  ·  ${bits.join(' · ')}` : m.id, info: m };
		});
		// Keep the typed value selectable even when the server does not list it —
		// remote gateways and proxies routinely rewrite the model name.
		if (p.model && !listed.some((o) => o.id === p.model)) {
			listed.unshift({ id: p.model, label: `${p.model}  ·  ${t('settings.probeUnlisted')}`, info: null });
		}
		return listed;
	}

	/** Server banner for a profile: build id, router capacity, resolved URL. */
	function serverLine(p: LlmProfile): string | null {
		const entry = modelLists[p.id];
		if (!entry?.ok || !entry.server) return null;
		const parts: string[] = [];
		if (entry.server.build_info) parts.push(entry.server.build_info);
		if (entry.server.max_instances) {
			parts.push(`${t('settings.probeMaxInstances')}: ${entry.server.max_instances}`);
		}
		if (entry.models_url) parts.push(entry.models_url);
		return parts.length ? parts.join(' · ') : null;
	}

	function profileApiKey(id: string): string | undefined {
		const draft = apiKeyDrafts[id];
		return draft && draft.trim() ? draft : undefined;
	}

	async function loadModels(s: Settings, p: LlmProfile) {
		ensureLlmDraft(s);
		try {
			modelLists = {
				...modelLists,
				[p.id]: await settings.llmModels({
					profile_id: p.id,
					base_url: p.base_url.trim(),
					api_key: profileApiKey(p.id),
				}),
			};
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			modelLists = {
				...modelLists,
				[p.id]: { ok: false, models: [], models_url: null, server: null, error: msg },
			};
		}
	}

	async function runProbe(s: Settings, p: LlmProfile) {
		ensureLlmDraft(s);
		if (!p.model.trim()) {
			toast.error(t('settings.requiredModel'));
			return;
		}
		probing = { ...probing, [p.id]: true };
		probeResults = { ...probeResults, [p.id]: null };
		try {
			// A cold llama.cpp endpoint loads the model before its first token —
			// a 27B can take well over a minute, so the backend needs the headroom.
			const result = await settings.llmTest({
				profile_id: p.id,
				base_url: p.base_url.trim(),
				model: p.model.trim(),
				api_key: profileApiKey(p.id),
				thinking: p.thinking ?? 'default',
				timeout: 240,
			});
			probeResults = { ...probeResults, [p.id]: result };
			modelLists = { ...modelLists, [p.id]: result };
			if (result.ok) toast.success(t('settings.probeOk'));
			else toast.error(t('settings.probeFailed'));
		} catch (err) {
			const msg = err instanceof Error ? err.message : String(err);
			probeResults = {
				...probeResults,
				[p.id]: {
					ok: false,
					reachable: false,
					models: [],
					models_url: null,
					server: null,
					error: msg,
					model: p.model.trim(),
					model_found: null,
					available_models: [],
					thinking: p.thinking ?? 'default',
					thinking_sent: {},
					chat_ok: false,
					latency_ms: null,
					first_token_ms: null,
					content: null,
					reasoning_chars: 0,
					reasoning_preview: null,
					usage: null,
				},
			};
			toast.error(msg);
		} finally {
			probing = { ...probing, [p.id]: false };
		}
	}
</script>

<svelte:window onbeforeunload={onBeforeUnload} />

<div class="shell">
	<AppHeader active="settings" crumb={'/ ' + t('nav.settings')}>
		{#snippet status()}
			{#if isDirty}
				<StatusChip status="paused" label={t('settings.unsaved')} />
			{/if}
		{/snippet}
	</AppHeader>

	<div class="body">
		<SettingsNav dirty={dirtyTabs} />
		<main class="content">
			{#if $settingsQuery.isLoading}
				<p class="muted">{t('settings.loading')}</p>
			{:else if $settingsQuery.data}
				{@const s = $settingsQuery.data}
				{#if tab === 'llm'}
					<section class="panel">
						<div class="panel-head">
							<div>
								<h1>{t('settings.llmSection')}</h1>
								<p class="lead">{t('settings.llmLead')}</p>
							</div>
							<Button variant="secondary" size="sm" onclick={() => addProfile(s)}>
								<Icon name="plus" size={14} />
								{t('settings.addLlm')}
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
											<span>{active ? t('settings.active') : t('settings.useThis')}</span>
										</label>
										{#if workingProfiles(s).length > 1}
											<Button
												variant="ghost"
												size="sm"
												title={t('settings.removeLlmTitle')}
												onclick={() => removeProfile(s, profile.id)}
											>
												<Icon name="trash" size={14} />
												{t('settings.remove')}
											</Button>
										{/if}
									</header>
									<label class="field">
										<span class="field-label">{t('settings.name')}</span>
										<input
											class="field-input"
											class:invalid={validationErrors[`llm_name_${profile.id}`]}
											value={profile.name}
											oninput={(e) => patchProfile(s, profile.id, { name: e.currentTarget.value })}
											placeholder={t('settings.llmNamePlaceholder')}
										/>
										{#if validationErrors[`llm_name_${profile.id}`]}
											<p class="field-error">{validationErrors[`llm_name_${profile.id}`]}</p>
										{/if}
									</label>
									<label class="field">
										<span class="field-label">{t('settings.baseUrl')}</span>
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
										<span class="field-label">{t('settings.model')}</span>
										{#if modelOptions(profile).length > 0}
											<select
												class="field-input"
												class:invalid={validationErrors[`llm_model_${profile.id}`]}
												value={profile.model}
												onchange={(e) =>
													patchProfile(s, profile.id, { model: e.currentTarget.value })}
											>
												{#each modelOptions(profile) as opt (opt.id)}
													<option value={opt.id}>{opt.label}</option>
												{/each}
											</select>
										{:else}
											<input
												class="field-input"
												class:invalid={validationErrors[`llm_model_${profile.id}`]}
												value={profile.model}
												oninput={(e) => patchProfile(s, profile.id, { model: e.currentTarget.value })}
												placeholder="llama3.2"
											/>
										{/if}
										{#if validationErrors[`llm_model_${profile.id}`]}
											<p class="field-error">{validationErrors[`llm_model_${profile.id}`]}</p>
										{/if}
										<div class="probe-actions">
											<Button variant="secondary" size="sm" onclick={() => loadModels(s, profile)}>
												<Icon name="refresh" size={14} />
												{t('settings.probeListModels')}
											</Button>
											<Button
												variant="secondary"
												size="sm"
												disabled={probing[profile.id] || !profile.model.trim()}
												onclick={() => runProbe(s, profile)}
											>
												<Icon name="activity" size={14} />
												{probing[profile.id] ? t('settings.probeRunning') : t('settings.probeTest')}
											</Button>
										</div>
										{#if modelLists[profile.id] && !modelLists[profile.id].ok}
											<p class="field-error">{modelLists[profile.id].error}</p>
										{:else if serverLine(profile)}
											<p class="field-hint">{serverLine(profile)}</p>
										{/if}
									</label>
									<label class="field">
										<span class="field-label">{t('settings.thinking')}</span>
										<select
											class="field-input"
											value={profile.thinking ?? 'default'}
											onchange={(e) =>
												patchProfile(s, profile.id, {
													thinking: e.currentTarget.value as ThinkingMode,
												})}
										>
											{#each THINKING_MODES as mode (mode)}
												<option value={mode}>{t(`settings.thinking_${mode}`)}</option>
											{/each}
										</select>
										<p class="field-hint">{t('settings.thinkingHint')}</p>
									</label>
									{#if probeResults[profile.id]}
										{@const probe = probeResults[profile.id]}
										<div class="probe-result" class:ok={probe?.ok} class:bad={probe && !probe.ok}>
											{#if probe?.ok}
												<p class="probe-line">
													✓ {t('settings.probeOk')}
													{#if probe.first_token_ms !== null}
														· {t('settings.probeTtft')}: {probe.first_token_ms} ms
													{/if}
													{#if probe.latency_ms !== null}
														· {t('settings.probeTotal')}: {probe.latency_ms} ms
													{/if}
												</p>
												{#if probe.reasoning_chars > 0}
													<p class="probe-line">
														{t('settings.probeReasoning')}: {probe.reasoning_chars}
														{#if probe.reasoning_preview}
															— {probe.reasoning_preview.slice(0, 120)}
														{/if}
													</p>
												{:else}
													<p class="probe-line muted">{t('settings.probeNoReasoning')}</p>
												{/if}
												{#if probe.content}
													<p class="probe-line muted">“{probe.content.slice(0, 160)}”</p>
												{/if}
											{:else}
												<p class="probe-line">✕ {probe?.error ?? t('settings.probeFailed')}</p>
											{/if}
										</div>
									{/if}
									<label class="field">
										<span class="field-label">{t('settings.apiKey')}</span>
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
												? t('settings.apiKeySaved')
												: t('settings.apiKeyOptional')}
										/>
										<p class="field-hint">{t('settings.apiKeyHint')}</p>
									</label>
								</article>
							{/each}
						</div>
					</section>
				{:else if tab === 'comfy'}
					<section class="panel">
						<h1>{t('settings.comfySection')}</h1>
						<p class="lead">{t('settings.comfyLead')}</p>
						<label class="field">
							<span class="field-label">{t('settings.baseUrl')}</span>
							<input
								class="field-input"
								value={String(fieldValue('comfyui_base_url', s.comfyui_base_url))}
								oninput={(e) => (draft.comfyui_base_url = e.currentTarget.value)}
							/>
							<p class="field-hint">{t('settings.comfyHint')}</p>
						</label>
						<label class="check">
							<input
								type="checkbox"
								checked={dryRunChecked(s)}
								onchange={(e) => (draft.dry_run = e.currentTarget.checked)}
							/>
							{t('settings.dryRunLabel')}
						</label>
					</section>
			{:else if tab === 'queue'}
				<section class="panel">
					<h1>{t('settings.queueSection')}</h1>
					<p class="lead">{t('settings.queueLead')}</p>
					<label class="field">
						<span class="field-label">{t('settings.concurrencyField')}</span>
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
						<span class="field-label">{t('settings.pollIntervalField')}</span>
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
						<span class="field-label">{t('settings.pollTimeoutField')}</span>
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
						<p class="field-hint">{t('settings.pollTimeoutHint')}</p>
					</label>
					<label class="field">
						<span class="field-label">{t('settings.maxRetriesField')}</span>
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
						<span class="field-label">{t('settings.agentMaxStepsField')}</span>
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
						<p class="field-hint">{t('settings.agentMaxStepsHint')}</p>
					</label>
				</section>
			{:else if tab === 'agent'}
				<section class="panel">
					<h1>{t('settings.agentModelsSection')}</h1>
					<p class="lead">{t('settings.agentModelsLead')}</p>
					{#each AGENT_ROLES as role (role.key)}
						<label class="field">
							<span class="field-label">{t(role.labelKey)}</span>
							<select
								class="field-input"
								value={assignmentValue(s, role.key)}
								onchange={(e) => setAssignment(role.key, e.currentTarget.value)}
							>
								<option value="">{t('settings.useActiveLlm')}</option>
								{#each workingProfiles(s) as p (p.id)}
									<option value={p.id}>{p.name} — {p.model}</option>
								{/each}
							</select>
							<p class="field-hint">{t(role.hintKey)}</p>
						</label>
					{/each}
				</section>
				<section class="panel">
					<h1>{t('settings.hardeningSection')}</h1>
						<p class="lead">
							{t('settings.hardeningLead')}
						</p>
						<div class="callout">
							<Icon name="alert" size={16} />
							<p>
								<strong>{t('settings.hardeningCalloutStrong')}</strong>{' '}
								{t('settings.hardeningCalloutBody')}
							</p>
						</div>
						<label class="field">
							<span class="field-label">{t('settings.hardeningRulesLabel')}</span>
							<textarea
								class="field-textarea mono"
								rows={18}
								spellcheck="false"
								value={hardeningDraft(s)}
								oninput={(e) => (draft.agent_hardening_prompt = e.currentTarget.value)}
								placeholder={t('settings.hardeningPlaceholder')}
							></textarea>
						<p class="field-hint">{t('settings.hardeningHint')}</p>
					</label>
			</section>
			<section class="panel">
				<h1>{t('settings.historyBudgetSection')}</h1>
				<p class="lead">{t('settings.historyBudgetLead')}</p>
				<label class="field">
					<span class="field-label">{t('settings.historyBudgetField')}</span>
					<input
						class="field-input"
						class:invalid={validationErrors.agent_history_char_budget}
						type="number"
						min="0"
						max="2000000"
						step="1000"
						value={String(fieldValue('agent_history_char_budget', s.agent_history_char_budget))}
						oninput={(e) => (draft.agent_history_char_budget = e.currentTarget.value)}
						onblur={(e) => clampOnBlur(e, 'agent_history_char_budget')}
					/>
					{#if validationErrors.agent_history_char_budget}
						<p class="field-error">{validationErrors.agent_history_char_budget}</p>
					{/if}
					<div class="preset-chips">
						{#each HISTORY_BUDGET_PRESETS as preset (preset.value)}
							<button
								type="button"
								class="preset-chip"
								class:active={historyBudgetActive(s, preset.value)}
								onclick={() => (draft.agent_history_char_budget = String(preset.value))}
							>
								{t(preset.labelKey)}
							</button>
						{/each}
					</div>
					<p class="field-hint">{t('settings.historyBudgetHint')}</p>
					<p class="field-hint">
						{t('settings.historyBudgetEffective', {
							chars: effectiveBudgetChars,
							tokens: effectiveBudgetTokens,
							ctx: s.context_window_tokens ?? 0,
						})}
					</p>
				</label>

				<div class="callout">
					<Icon name="info" size={16} />
					<p>{t('settings.contextLead')}</p>
				</div>

				<label class="field">
					<span class="field-label">{t('settings.maxOutputField')}</span>
					<input
						class="field-input"
						class:invalid={validationErrors.llm_max_output_tokens}
						type="number"
						min="0"
						max="200000"
						step="256"
						value={String(fieldValue('llm_max_output_tokens', s.llm_max_output_tokens))}
						oninput={(e) => (draft.llm_max_output_tokens = e.currentTarget.value)}
						onblur={(e) => clampOnBlur(e, 'llm_max_output_tokens')}
					/>
					{#if validationErrors.llm_max_output_tokens}
						<p class="field-error">{validationErrors.llm_max_output_tokens}</p>
					{/if}
					<p class="field-hint">{t('settings.maxOutputHint')}</p>
				</label>

				<label class="field">
					<span class="field-label">{t('settings.historyShareField')}</span>
					<input
						class="field-input"
						class:invalid={validationErrors.agent_history_token_share}
						type="number"
						min="0.05"
						max="0.95"
						step="0.05"
						value={String(fieldValue('agent_history_token_share', s.agent_history_token_share))}
						oninput={(e) => (draft.agent_history_token_share = e.currentTarget.value)}
						onblur={(e) => clampOnBlur(e, 'agent_history_token_share')}
					/>
					{#if validationErrors.agent_history_token_share}
						<p class="field-error">{validationErrors.agent_history_token_share}</p>
					{/if}
					<p class="field-hint">{t('settings.historyShareHint')}</p>
				</label>

				<label class="field">
					<span class="field-label">{t('settings.charsPerTokenField')}</span>
					<input
						class="field-input"
						class:invalid={validationErrors.llm_chars_per_token}
						type="number"
						min="0.5"
						max="8"
						step="0.1"
						value={String(fieldValue('llm_chars_per_token', s.llm_chars_per_token))}
						oninput={(e) => (draft.llm_chars_per_token = e.currentTarget.value)}
						onblur={(e) => clampOnBlur(e, 'llm_chars_per_token')}
					/>
					{#if validationErrors.llm_chars_per_token}
						<p class="field-error">{validationErrors.llm_chars_per_token}</p>
					{/if}
					<p class="field-hint">{t('settings.charsPerTokenHint')}</p>
				</label>

				<label class="field">
					<span class="field-label">{t('settings.fallbackCtxField')}</span>
					<input
						class="field-input"
						class:invalid={validationErrors.llm_context_fallback_tokens}
						type="number"
						min="1024"
						max="10000000"
						step="1024"
						value={String(fieldValue('llm_context_fallback_tokens', s.llm_context_fallback_tokens))}
						oninput={(e) => (draft.llm_context_fallback_tokens = e.currentTarget.value)}
						onblur={(e) => clampOnBlur(e, 'llm_context_fallback_tokens')}
					/>
					{#if validationErrors.llm_context_fallback_tokens}
						<p class="field-error">{validationErrors.llm_context_fallback_tokens}</p>
					{/if}
					<p class="field-hint">{t('settings.fallbackCtxHint')}</p>
				</label>
			</section>
			<section class="panel">
				<h1>{t('settings.shellSection')}</h1>
					<p class="lead">{t('settings.shellLead')}</p>
					<div class="callout">
						<Icon name="alert" size={16} />
						<p>
							<strong>{t('settings.shellCalloutStrong')}</strong>{' '}
							{t('settings.shellCalloutBody')}
						</p>
					</div>
					<label class="check">
						<input
							type="checkbox"
							checked={shellChecked(s)}
							onchange={(e) => (draft.agent_shell_enabled = e.currentTarget.checked)}
						/>
						{t('settings.shellLabel')}
					</label>
					<p class="field-hint">{t('settings.shellHint')}</p>
				</section>
				<MemoryPanel />
				{:else if tab === 'storage'}
					<section class="panel">
						<h1>{t('settings.storageSection')}</h1>
						<p class="lead">{t('settings.storageLead')}</p>
						<div class="callout">
							<Icon name="alert" size={16} />
							<p>
								<strong>{t('settings.storageCalloutStrong')}</strong>{' '}
								{t('settings.storageCalloutBody')}
							</p>
						</div>
						<label class="field">
							<span class="field-label">{t('settings.dataDir')}</span>
							<input
								class="field-input"
								value={String(fieldValue('data_dir', s.data_dir))}
								oninput={(e) => (draft.data_dir = e.currentTarget.value)}
							/>
							<p class="field-hint">{t('settings.current')} <code class="mono">{s.data_dir}</code></p>
						</label>
						<label class="field">
							<span class="field-label">{t('settings.assetsDir')}</span>
							<input
								class="field-input"
								value={String(fieldValue('assets_dir', s.assets_dir))}
								oninput={(e) => (draft.assets_dir = e.currentTarget.value)}
							/>
							<p class="field-hint">{t('settings.current')} <code class="mono">{s.assets_dir}</code></p>
						</label>
						<label class="field">
							<span class="field-label">{t('settings.workspaceDir')}</span>
							<input
								class="field-input"
								value={String(fieldValue('agent_workspace_dir', s.agent_workspace_dir))}
								oninput={(e) => (draft.agent_workspace_dir = e.currentTarget.value)}
							/>
							<p class="field-hint">{t('settings.workspaceDirHint')}</p>
							<p class="field-hint">{t('settings.current')} <code class="mono">{s.agent_workspace_dir}</code></p>
						</label>
					</section>
				{:else if tab === 'workflows'}
					<WorkflowsLibrary />
				{:else if tab === 'skills'}
					<SkillsLibrary />
				{/if}

				{#if tab !== 'workflows' && tab !== 'skills'}
					<div class="save-bar">
						<span class="save-state" class:dirty={isDirty}>
							{#if isDirty}
								<span class="save-dot" aria-hidden="true"></span>{t('settings.unsaved')}
							{:else}
								{t('settings.allSaved')}
							{/if}
						</span>
						<Button
							variant="ghost"
							disabled={!isDirty || $saveMutation.isPending}
							onclick={discardDraft}
						>
							{t('settings.discard')}
						</Button>
					<Button
						variant="primary"
						disabled={!isDirty || !isValid}
						loading={$saveMutation.isPending}
						onclick={() => $saveMutation.mutate()}
					>
						{t('settings.saveChanges')}
					</Button>
					</div>
				{/if}
			{/if}
		</main>
	</div>
</div>

<ConfirmDialog
	bind:open={leaveOpen}
	title={t('settings.leaveTitle')}
	message={t('settings.leaveMessage')}
	confirmLabel={t('settings.leaveConfirm')}
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
	.preset-chips {
		display: flex;
		gap: 8px;
		margin-top: 8px;
		flex-wrap: wrap;
	}
	.preset-chip {
		border: 1px solid var(--border);
		background: transparent;
		color: var(--text-secondary);
		border-radius: 999px;
		padding: 4px 12px;
		font-size: 12px;
		cursor: pointer;
		transition: border-color 0.15s, color 0.15s, background 0.15s;
	}
	.preset-chip:hover {
		border-color: var(--accent);
		color: var(--text-primary);
	}
	.preset-chip.active {
		border-color: var(--accent);
		background: var(--accent);
		color: var(--accent-contrast, #0b0e14);
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
	.probe-actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-top: 8px;
	}
	.probe-result {
		margin: 10px 0 2px;
		padding: 10px 12px;
		border: 1px solid rgba(255, 255, 255, 0.08);
		border-left-width: 3px;
		border-radius: var(--radius-sm, 6px);
		background: rgba(0, 0, 0, 0.22);
		font-size: 12px;
		line-height: 1.6;
	}
	.probe-result.ok {
		border-left-color: var(--success, #22c55e);
	}
	.probe-result.bad {
		border-left-color: var(--error);
	}
	.probe-line {
		margin: 0;
		color: var(--text-primary);
		word-break: break-word;
	}
	.probe-line.muted {
		color: var(--text-muted);
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
