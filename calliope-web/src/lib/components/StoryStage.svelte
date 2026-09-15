<script lang="ts">
	import { goto } from '$app/navigation';
	import { createMutation, useQueryClient } from '@tanstack/svelte-query';
	import {
		projects,
		type Beat,
		type Character,
		type Item,
		type Location,
		type StoryData,
	} from '$lib/api';
	import {
		buildStoryDraftPromptPreview,
		estimateTargetSeconds,
		recommendBeatCount,
		recommendSceneCount,
	} from '$lib/durationBudget';
	import { toast } from '$lib/toast';
	import { t } from '$lib/i18n.svelte';
	import { agentDeepLink } from '$lib/agentTasks';
	import Button from '$lib/components/ui/Button.svelte';
	import Card from '$lib/components/ui/Card.svelte';
	import StatusChip from '$lib/components/ui/StatusChip.svelte';
	import Modal from '$lib/components/ui/Modal.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import EmptyState from '$lib/components/ui/EmptyState.svelte';
	import Spinner from '$lib/components/ui/Spinner.svelte';
	import Icon from '$lib/components/ui/Icon.svelte';

	interface Props {
		projectId: number;
		story: StoryData;
		configured: boolean;
		onGoAssets?: () => void;
	}

	let { projectId, story, configured, onGoAssets }: Props = $props();
	const client = useQueryClient();

	const GENRES = [
		'Adventure / Mystery',
		'Drama',
		'Sci-Fi',
		'Fantasy',
		'Horror',
		'Romance',
		'Thriller',
	];
	const LENGTHS_HINT = $derived(t('story.lengthsHint'));
	const TONES = [
		'Cinematic, atmospheric',
		'Dark, tense',
		'Whimsical, warm',
		'Gritty, realistic',
		'Epic, sweeping',
	];

	const GENRE_KEYS: Record<string, string> = {
		'Adventure / Mystery': 'genres.adventure',
		Drama: 'genres.drama',
		'Sci-Fi': 'genres.scifi',
		Fantasy: 'genres.fantasy',
		Horror: 'genres.horror',
		Romance: 'genres.romance',
		Thriller: 'genres.thriller',
	};
	const TONE_KEYS: Record<string, string> = {
		'Cinematic, atmospheric': 'tones.cinematic',
		'Dark, tense': 'tones.dark',
		'Whimsical, warm': 'tones.whimsical',
		'Gritty, realistic': 'tones.gritty',
		'Epic, sweeping': 'tones.epic',
	};
	function genreLabel(v: string): string {
		return t(GENRE_KEYS[v] ?? v);
	}
	function toneLabel(v: string): string {
		return t(TONE_KEYS[v] ?? v);
	}

	const EXAMPLE = {
		idea: 'A lone cartographer discovers a map that rewrites itself every midnight, leading her into a forgotten city beneath the desert.',
		genre: 'Adventure / Mystery',
		tone: 'Cinematic, atmospheric',
		target_duration: '2 minutes',
	};

	let editingBeat = $state<Beat | null>(null);
	let editingChar = $state<Character | null>(null);
	let editingLoc = $state<Location | null>(null);
	let editingItem = $state<Item | null>(null);
	let beatModalOpen = $state(false);
	let charModalOpen = $state(false);
	let locModalOpen = $state(false);
	let itemModalOpen = $state(false);
	let confirmExampleOpen = $state(false);
	let deletingBeat = $state<Beat | null>(null);
	let beatDeleteOpen = $state(false);
	let addingBeat = $state(false);
	// Static defaults — the $effect below syncs all drafts from the story prop.
	let ideaDraft = $state('');
	let genreDraft = $state(GENRES[0]);
	let toneDraft = $state(TONES[0]);
	let lengthDraft = $state('2 minutes');
	let showDraftPrompt = $state(false);

	type SaveState = 'idle' | 'saving' | 'saved' | 'error';
	let saveState = $state<SaveState>('idle');
	let savedTimer: ReturnType<typeof setTimeout> | null = null;

	const beatBudget = $derived(recommendBeatCount(lengthDraft));
	const sceneBudget = $derived(recommendSceneCount(lengthDraft));
	const secsBudget = $derived(estimateTargetSeconds(lengthDraft));
	const draftPromptPreview = $derived(
		buildStoryDraftPromptPreview({
			title: story.project.title,
			idea: ideaDraft,
			genre: genreDraft,
			tone: toneDraft,
			targetDuration: lengthDraft,
		}),
	);

	$effect(() => {
		ideaDraft = story.project.idea ?? '';
		genreDraft = story.project.genre || GENRES[0];
		toneDraft = story.project.tone || TONES[0];
		lengthDraft = story.project.target_duration || '2 minutes';
	});

	$effect(() => {
		return () => {
			if (savedTimer) clearTimeout(savedTimer);
		};
	});

	function armSavedTimer() {
		if (savedTimer) clearTimeout(savedTimer);
		savedTimer = setTimeout(() => {
			if (saveState === 'saved') saveState = 'idle';
		}, 2000);
	}

	const saveProject = createMutation({
		mutationFn: (payload: {
			idea?: string;
			genre?: string;
			tone?: string;
			target_duration?: string;
		}) => projects.update(projectId, payload),
		onMutate: () => {
			saveState = 'saving';
		},
		onSuccess: () => {
			client.invalidateQueries({ queryKey: ['story'] });
			saveState = 'saved';
			armSavedTimer();
		},
		onError: (err) => {
			saveState = 'error';
			toast.error(err instanceof Error ? err.message : t('story.saveSettingsFailed'));
		},
	});

	/** Autosave path — returns false on failure (mutation onError already toasted). */
	async function persistSettings(): Promise<boolean> {
		try {
			await $saveProject.mutateAsync({
				idea: ideaDraft,
				genre: genreDraft || undefined,
				tone: toneDraft || undefined,
				target_duration: lengthDraft || undefined,
			});
			return true;
		} catch {
			return false;
		}
	}

	async function loadExample() {
		ideaDraft = EXAMPLE.idea;
		genreDraft = EXAMPLE.genre;
		toneDraft = EXAMPLE.tone;
		lengthDraft = EXAMPLE.target_duration;
		const saved = await persistSettings();
		if (saved) toast.success(t('story.exampleLoaded'));
	}

	function requestLoadExample() {
		if (ideaDraft.trim()) confirmExampleOpen = true;
		else void loadExample();
	}

	/** Hand off to a fresh, project-linked agent chat with the composer pre-filled. */
	async function draftStoryline() {
		if (!configured) return;
		const saved = await persistSettings();
		if (!saved) return; // persistSettings already toasted the failure
		goto(agentDeepLink(projectId, 'story'));
	}

	function openBeat(beat: Beat) {
		editingBeat = { ...beat };
		beatModalOpen = true;
	}

	async function addBeat() {
		if (addingBeat) return;
		addingBeat = true;
		try {
			const nextIndex =
				story.beats.reduce((max, b) => Math.max(max, b.order_index), 0) + 1;
			// No api.ts binding exists yet for POST /beats — call the endpoint directly.
			const res = await fetch(`/api/projects/${projectId}/beats`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({
					order_index: nextIndex,
					title: t('story.newBeat', { n: nextIndex }),
					description: '',
				}),
			});
			if (!res.ok) {
				const body = await res.text().catch(() => 'unknown error');
				throw new Error(`${res.status}: ${body}`);
			}
			const created = (await res.json()) as Beat;
			await client.invalidateQueries({ queryKey: ['story'] });
			toast.success(t('story.beatAdded', { index: nextIndex }));
			openBeat(created);
		} catch (err) {
			toast.error(err instanceof Error ? err.message : t('story.addBeatFailed'));
		} finally {
			addingBeat = false;
		}
	}

	function requestDeleteBeat(beat: Beat) {
		deletingBeat = beat;
		beatDeleteOpen = true;
	}

	const deleteBeatMutation = createMutation({
		mutationFn: (beatId: number) => projects.deleteBeat(projectId, beatId),
		onSuccess: () => {
			client.invalidateQueries({ queryKey: ['story'] });
			toast.success(t('story.beatDeleted'));
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('story.deleteBeatFailed'));
		},
	});

	const saveBeat = createMutation({
		mutationFn: () =>
			projects.updateBeat(projectId, editingBeat!.id, {
				title: editingBeat!.title,
				description: editingBeat!.description,
				order_index: editingBeat!.order_index,
			}),
		onSuccess: () => {
			editingBeat = null;
			beatModalOpen = false;
			client.invalidateQueries({ queryKey: ['story'] });
			toast.success(t('story.beatSaved'));
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('story.saveBeatFailed'));
		},
	});

	const saveChar = createMutation({
		mutationFn: () =>
			projects.updateCharacter(projectId, editingChar!.id, {
				name: editingChar!.name,
				role: editingChar!.role,
				age: editingChar!.age,
				appearance: editingChar!.appearance,
				personality: editingChar!.personality,
				consistency_prompt: editingChar!.consistency_prompt,
			}),
		onSuccess: () => {
			editingChar = null;
			charModalOpen = false;
			client.invalidateQueries({ queryKey: ['story'] });
			toast.success(t('story.characterSaved'));
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('story.saveCharacterFailed'));
		},
	});

	const saveLoc = createMutation({
		mutationFn: () =>
			projects.updateLocation(projectId, editingLoc!.id, {
				name: editingLoc!.name,
				description: editingLoc!.description,
				consistency_prompt: editingLoc!.consistency_prompt,
			}),
		onSuccess: () => {
			editingLoc = null;
			locModalOpen = false;
			client.invalidateQueries({ queryKey: ['story'] });
			toast.success(t('story.locationSaved'));
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('story.saveLocationFailed'));
		},
	});

	const saveItem = createMutation({
		mutationFn: () =>
			projects.updateItem(projectId, editingItem!.id, {
				name: editingItem!.name,
				description: editingItem!.description,
				consistency_prompt: editingItem!.consistency_prompt,
			}),
		onSuccess: () => {
			editingItem = null;
			itemModalOpen = false;
			client.invalidateQueries({ queryKey: ['story'] });
			toast.success(t('story.itemSaved'));
		},
		onError: (err) => {
			toast.error(err instanceof Error ? err.message : t('story.saveItemFailed'));
		},
	});

	function goToAssets() {
		goto('?stage=assets', { keepFocus: true, noScroll: true });
	}

	function beatCountLabel(n: number): string {
		return t('story.beats', { n });
	}
</script>

{#snippet saveIndicator()}
	{#if saveState === 'saving'}
		<span class="save-ind"><Spinner size="sm" /> {t('common.saving')}</span>
	{:else if saveState === 'saved'}
		<span class="save-ind saved"><Icon name="check" size={13} /> {t('common.saved')}</span>
	{:else if saveState === 'error'}
		<span class="save-ind err">
			<Icon name="alert" size={13} /> {t('common.saveFailed')}
			<Button variant="ghost" size="sm" onclick={() => void persistSettings()}>{t('common.retry')}</Button>
		</span>
	{/if}
{/snippet}

<header class="stage-header">
	<h2>{t('story.stageTitle')}</h2>
	<div class="stage-actions">
		<Button
			variant="secondary"
			disabled={$saveProject.isPending}
			onclick={requestLoadExample}
		>
			{t('story.loadExample')}
		</Button>
		<Button variant="primary" disabled={!configured} onclick={() => void draftStoryline()}>
			<Icon name="sparkle" size={15} /> {t('story.draftStoryline')}
		</Button>
	</div>
</header>

{#if !configured}
	<div class="banner">{t('story.llmBanner')}</div>
{/if}

<div class="stack-col">
	<Card>
	{#snippet header()}
		<h3 class="card-h">{t('story.ideaTitle')}</h3>
		{@render saveIndicator()}
	{/snippet}
	<textarea
		class="field-textarea"
		bind:value={ideaDraft}
		rows="5"
		placeholder={t('story.ideaPlaceholder')}
		onblur={() => void persistSettings()}
	></textarea>
</Card>

<Card>
	{#snippet header()}
		<h3 class="card-h">{t('story.settingsTitle')}</h3>
		{@render saveIndicator()}
	{/snippet}
	<div class="grid-3">
		<label class="field">
			<span class="field-label">{t('projects.genre')}</span>
			<select
				class="field-select"
				bind:value={genreDraft}
				onchange={() => void persistSettings()}
			>
{#each GENRES as g}
					<option value={g}>{genreLabel(g)}</option>
				{/each}
				{#if genreDraft && !GENRES.includes(genreDraft)}
					<option value={genreDraft}>{genreLabel(genreDraft)}</option>
				{/if}
			</select>
		</label>
		<label class="field">
			<span class="field-label">{t('story.targetLength')}</span>
			<input
				class="field-input"
				type="text"
				bind:value={lengthDraft}
				placeholder={LENGTHS_HINT}
				onblur={() => void persistSettings()}
			/>
			<span class="field-hint beat-hint">
				{t('story.lengthHint', { secs: secsBudget, beats: beatBudget, scenes: sceneBudget })}
			</span>
		</label>
		<label class="field">
			<span class="field-label">{t('projects.tone')}</span>
			<select
				class="field-select"
				bind:value={toneDraft}
				onchange={() => void persistSettings()}
			>
				{#each TONES as tone}
					<option value={tone}>{toneLabel(tone)}</option>
				{/each}
				{#if toneDraft && !TONES.includes(toneDraft)}
					<option value={toneDraft}>{toneLabel(toneDraft)}</option>
				{/if}
			</select>
		</label>
	</div>
</Card>

<Card>
	{#snippet header()}
		<h3 class="card-h">{t('story.draftPromptTitle')}</h3>
		<Button variant="ghost" size="sm" onclick={() => (showDraftPrompt = !showDraftPrompt)}>
			{showDraftPrompt ? t('story.hidePrompt') : t('story.showPrompt')}
		</Button>
	{/snippet}
	{#if showDraftPrompt}
		<pre class="prompt-preview">{draftPromptPreview}</pre>
		<p class="field-hint">
			{t('story.promptExplain')}
		</p>
	{:else}
		<p class="field-hint prompt-collapsed">
			{t('story.promptCollapsedHint')}
		</p>
	{/if}
</Card>

<Card>
	{#snippet header()}
		<h3 class="card-h">{t('story.beatsTitle')}</h3>
		<div class="head-actions">
			{#if story.beats.length}
				<StatusChip status="ready" label={beatCountLabel(story.beats.length)} />
			{/if}
			<Button variant="secondary" size="sm" loading={addingBeat} onclick={addBeat}>
				<Icon name="plus" size={14} /> {t('story.addBeat')}
			</Button>
		</div>
	{/snippet}
	{#if story.beats.length === 0}
		<EmptyState
			title={t('story.noBeats')}
			body={t('story.noBeatsBody')}
		>
			{#snippet icon()}
				<Icon name="story" size={28} />
			{/snippet}
			{#snippet action()}
				<Button variant="secondary" size="sm" loading={addingBeat} onclick={addBeat}>
					<Icon name="plus" size={14} /> {t('story.addBeatManual')}
				</Button>
			{/snippet}
		</EmptyState>
	{:else}
		<div class="beat-list">
			{#each story.beats as beat (beat.id)}
				<div class="beat-row">
					<button type="button" class="beat" onclick={() => openBeat(beat)}>
						<div class="beat-number">{beat.order_index}</div>
						<div class="beat-body">
							<h4>{beat.title}</h4>
							<p>{beat.description}</p>
						</div>
					</button>
					<div class="beat-actions">
						<Button
							variant="ghost"
							size="sm"
							title={t('story.deleteBeat', { index: beat.order_index })}
							onclick={() => requestDeleteBeat(beat)}
						>
							<Icon name="trash" size={14} /><span class="sr-only">{t('story.deleteBeat', { index: beat.order_index })}</span>
						</Button>
					</div>
				</div>
			{/each}
		</div>
		<div class="beats-footer">
			<Button variant="primary" onclick={goToAssets}>
				{t('story.continueAssets')} <Icon name="chevron-right" size={15} />
			</Button>
		</div>
	{/if}
</Card>

{#if story.characters.length || story.locations.length || story.items.length}
	<Card>
		{#snippet header()}
			<h3 class="card-h">{t('story.extracted')}</h3>
			<Button variant="ghost" size="sm" onclick={goToAssets}>
				{t('story.openAssets')} <Icon name="chevron-right" size={14} />
			</Button>
		{/snippet}
		<div class="chips">
			{#each story.characters as char (char.id)}
				<button
					type="button"
					class="chip"
					onclick={() => {
						editingChar = { ...char };
						charModalOpen = true;
					}}
					title={char.appearance ?? ''}
				>
					<span class="chip-tag">{t('story.charTag')}</span>
					{char.name}
				</button>
			{/each}
			{#each story.locations as loc (loc.id)}
				<button
					type="button"
					class="chip"
					onclick={() => {
						editingLoc = { ...loc };
						locModalOpen = true;
					}}
					title={loc.description ?? ''}
				>
					<span class="chip-tag">{t('story.envTag')}</span>
					{loc.name}
				</button>
			{/each}
			{#each story.items as item (item.id)}
				<button
					type="button"
					class="chip"
					onclick={() => {
						editingItem = { ...item };
						itemModalOpen = true;
					}}
					title={item.description ?? ''}
				>
					<span class="chip-tag">{t('story.itemTag')}</span>
					{item.name}
				</button>
			{/each}
		</div>
	</Card>
{/if}
</div>

<Modal bind:open={beatModalOpen} title={t('story.editBeatTitle')} onclose={() => (editingBeat = null)}>
	{#if editingBeat}
		<label class="field">
			<span class="field-label">{t('story.fieldTitle')}</span>
			<input class="field-input" bind:value={editingBeat.title} />
		</label>
		<label class="field">
			<span class="field-label">{t('story.fieldDescription')}</span>
			<textarea class="field-textarea" rows="4" bind:value={editingBeat.description}></textarea>
		</label>
	{/if}
	{#snippet footer()}
		<Button variant="ghost" onclick={() => (beatModalOpen = false)}>{t('common.cancel')}</Button>
		<Button variant="primary" loading={$saveBeat.isPending} onclick={() => $saveBeat.mutate()}>
			{t('common.save')}
		</Button>
	{/snippet}
</Modal>

<Modal bind:open={charModalOpen} title={t('story.editCharTitle')} onclose={() => (editingChar = null)}>
	{#if editingChar}
		<label class="field">
			<span class="field-label">{t('story.fieldName')}</span>
			<input class="field-input" bind:value={editingChar.name} placeholder={t('story.fieldName')} />
		</label>
		<label class="field">
			<span class="field-label">{t('story.fieldRole')}</span>
			<input class="field-input" bind:value={editingChar.role} placeholder={t('story.fieldRole')} />
		</label>
		<label class="field">
			<span class="field-label">{t('story.fieldAge')}</span>
			<input class="field-input" bind:value={editingChar.age} placeholder={t('story.fieldAge')} />
		</label>
		<label class="field">
			<span class="field-label">{t('story.fieldAppearance')}</span>
			<textarea
				class="field-textarea"
				rows="3"
				bind:value={editingChar.appearance}
				placeholder={t('story.fieldAppearance')}
			></textarea>
		</label>
		<label class="field">
			<span class="field-label">{t('story.fieldPersonality')}</span>
			<textarea
				class="field-textarea"
				rows="2"
				bind:value={editingChar.personality}
				placeholder={t('story.fieldPersonality')}
			></textarea>
		</label>
		<label class="field">
			<span class="field-label">{t('story.fieldConsistency')}</span>
			<textarea
				class="field-textarea"
				rows="2"
				bind:value={editingChar.consistency_prompt}
				placeholder={t('story.fieldConsistency')}
			></textarea>
		</label>
	{/if}
	{#snippet footer()}
		<Button variant="ghost" onclick={() => (charModalOpen = false)}>{t('common.cancel')}</Button>
		<Button variant="primary" loading={$saveChar.isPending} onclick={() => $saveChar.mutate()}>
			{t('common.save')}
		</Button>
	{/snippet}
</Modal>

<Modal bind:open={locModalOpen} title={t('story.editLocTitle')} onclose={() => (editingLoc = null)}>
	{#if editingLoc}
		<label class="field">
			<span class="field-label">{t('story.fieldName')}</span>
			<input class="field-input" bind:value={editingLoc.name} placeholder={t('story.fieldName')} />
		</label>
		<label class="field">
			<span class="field-label">{t('story.fieldDescription')}</span>
			<textarea
				class="field-textarea"
				rows="3"
				bind:value={editingLoc.description}
				placeholder={t('story.fieldDescription')}
			></textarea>
		</label>
		<label class="field">
			<span class="field-label">{t('story.fieldConsistency')}</span>
			<textarea
				class="field-textarea"
				rows="2"
				bind:value={editingLoc.consistency_prompt}
				placeholder={t('story.fieldConsistency')}
			></textarea>
		</label>
	{/if}
	{#snippet footer()}
		<Button variant="ghost" onclick={() => (locModalOpen = false)}>{t('common.cancel')}</Button>
		<Button variant="primary" loading={$saveLoc.isPending} onclick={() => $saveLoc.mutate()}>
			{t('common.save')}
		</Button>
	{/snippet}
</Modal>

<Modal bind:open={itemModalOpen} title={t('story.editItemTitle')} onclose={() => (editingItem = null)}>
	{#if editingItem}
		<label class="field">
			<span class="field-label">{t('story.fieldName')}</span>
			<input class="field-input" bind:value={editingItem.name} placeholder={t('story.fieldName')} />
		</label>
		<label class="field">
			<span class="field-label">{t('story.fieldDescription')}</span>
			<textarea
				class="field-textarea"
				rows="3"
				bind:value={editingItem.description}
				placeholder={t('story.fieldDescription')}
			></textarea>
		</label>
		<label class="field">
			<span class="field-label">{t('story.fieldConsistency')}</span>
			<textarea
				class="field-textarea"
				rows="2"
				bind:value={editingItem.consistency_prompt}
				placeholder={t('story.fieldConsistency')}
			></textarea>
		</label>
	{/if}
	{#snippet footer()}
		<Button variant="ghost" onclick={() => (itemModalOpen = false)}>{t('common.cancel')}</Button>
		<Button variant="primary" loading={$saveItem.isPending} onclick={() => $saveItem.mutate()}>
			{t('common.save')}
		</Button>
	{/snippet}
</Modal>

<ConfirmDialog
	bind:open={confirmExampleOpen}
	title={t('story.confirmExampleTitle')}
	message={t('story.confirmExampleMsg')}
	confirmLabel={t('story.loadExample')}
	onconfirm={() => void loadExample()}
/>

<ConfirmDialog
	bind:open={beatDeleteOpen}
	title={t('story.confirmDeleteBeatTitle')}
	message={deletingBeat
		? t('story.confirmDeleteBeatMsg', { index: deletingBeat.order_index, title: deletingBeat.title })
		: ''}
	confirmLabel={t('common.delete')}
	danger
	onconfirm={() => {
		const id = deletingBeat?.id;
		deletingBeat = null;
		if (id != null) $deleteBeatMutation.mutate(id);
	}}
	oncancel={() => (deletingBeat = null)}
/>

<style>
	.stage-header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		margin-bottom: var(--space-lg);
		gap: var(--space-md);
	}
	.stage-header h2 {
		margin: 0;
		font-size: 22px;
		font-weight: 700;
	}
	.stage-actions {
		display: flex;
		gap: var(--space-sm);
	}
	.banner {
		background: rgba(245, 158, 11, 0.1);
		border: 1px solid rgba(245, 158, 11, 0.3);
		color: var(--warning);
		padding: var(--space-md);
		border-radius: var(--radius-md);
		margin-bottom: var(--space-lg);
		font-size: 13px;
	}
	.stack-col {
		position: relative;
		display: flex;
		flex-direction: column;
		gap: var(--space-md);
	}
	.card-h {
		margin: 0;
		font-size: 16px;
		color: var(--text-secondary);
		font-weight: 600;
	}
	.head-actions {
		display: flex;
		align-items: center;
		gap: var(--space-sm);
	}
	.save-ind {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 12px;
		color: var(--text-muted);
	}
	.save-ind.saved {
		color: var(--success);
		animation: saved-fade 2s ease forwards;
	}
	.save-ind.err {
		color: var(--error);
	}
	@keyframes saved-fade {
		0% {
			opacity: 0;
		}
		12% {
			opacity: 1;
		}
		78% {
			opacity: 1;
		}
		100% {
			opacity: 0;
		}
	}
	.grid-3 {
		display: grid;
		grid-template-columns: repeat(3, 1fr);
		gap: var(--space-md);
	}
	@media (max-width: 900px) {
		.grid-3 {
			grid-template-columns: 1fr;
		}
	}
	.grid-3 .field {
		margin-bottom: 0;
	}
	.beat-hint {
		display: block;
	}
	.beat-hint strong {
		color: var(--accent);
		font-weight: 600;
	}
	.prompt-preview {
		margin: 0 0 var(--space-sm);
		padding: 12px;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		font-family: var(--font-mono);
		font-size: 11px;
		line-height: 1.45;
		white-space: pre-wrap;
		color: var(--text-secondary);
		max-height: 280px;
		overflow: auto;
	}
	.prompt-collapsed {
		margin: 0;
	}
	.beat-list {
		display: flex;
		flex-direction: column;
		gap: 10px;
	}
	.beat-row {
		display: flex;
		align-items: flex-start;
		gap: var(--space-xs);
	}
	.beat {
		display: flex;
		flex: 1;
		min-width: 0;
		gap: var(--space-md);
		align-items: flex-start;
		background: transparent;
		border: 1px solid transparent;
		border-radius: var(--radius-md);
		padding: 8px;
		cursor: pointer;
		text-align: left;
		color: inherit;
		font: inherit;
	}
	.beat:hover {
		border-color: var(--border);
		background: var(--bg-elevated);
	}
	.beat:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.beat-actions {
		flex-shrink: 0;
		padding-top: 8px;
	}
	.beat-number {
		width: 36px;
		height: 36px;
		border-radius: var(--radius-md);
		background: var(--bg-elevated);
		display: flex;
		align-items: center;
		justify-content: center;
		font-weight: 700;
		color: var(--text-secondary);
		flex-shrink: 0;
	}
	.beat-body h4 {
		margin: 0 0 4px;
		font-size: 15px;
		font-weight: 600;
	}
	.beat-body p {
		margin: 0;
		color: var(--text-secondary);
		font-size: 14px;
		line-height: 1.45;
	}
	.beats-footer {
		display: flex;
		justify-content: flex-end;
		margin-top: var(--space-md);
		padding-top: var(--space-md);
		border-top: 1px solid var(--border);
	}
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
	}
	.chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		background: var(--bg-elevated);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 6px 10px;
		color: var(--text-primary);
		font-size: 13px;
		cursor: pointer;
	}
	.chip:hover {
		border-color: var(--accent);
	}
	.chip:focus-visible {
		outline: 2px solid var(--accent);
		outline-offset: 2px;
	}
	.chip-tag {
		font-size: 10px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--accent);
		font-weight: 700;
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
