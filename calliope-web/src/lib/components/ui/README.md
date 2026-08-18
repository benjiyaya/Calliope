# Calliope UI primitives

Shared component library for the Calliope UI. Import from `$lib/components/ui/`.
Svelte 5 runes mode, TypeScript strict, scoped styles driven by the CSS vars in
`src/routes/app.css`. No Tailwind.

Conventions shared by all primitives:

- Styling uses the global tokens (`--bg-surface`, `--border`, `--accent`, `--radius-*`, `--space-*`, fonts). Never hardcode hex colors outside this directory.
- All interactive primitives expose a `:focus-visible` accent ring. `app.css` also provides a global `:focus-visible` fallback for native elements.
- Motion respects `prefers-reduced-motion`: `app.css` kills animation/transition durations globally; components that need a styled static fallback (Skeleton, ProgressBar indeterminate, StatusChip pulse) handle it in their own scoped styles.

---

## Button

`import Button from '$lib/components/ui/Button.svelte';`

| Prop       | Type                                              | Default       | Notes                                              |
| ---------- | ------------------------------------------------- | ------------- | -------------------------------------------------- |
| `variant`  | `'primary' \| 'secondary' \| 'ghost' \| 'danger'` | `'secondary'` | primary = `--accent` bg; danger = `--error` tinted |
| `size`     | `'sm' \| 'md'`                                    | `'md'`        | sm = 28px, md = 34px min-height                    |
| `disabled` | `boolean`                                         | `false`       | reduced opacity, no pointer events                 |
| `loading`  | `boolean`                                         | `false`       | inline spinner, disables, width stays stable       |
| `type`     | `'button' \| 'submit' \| 'reset'`                 | `'button'`    |                                                    |
| `title`    | `string`                                          | —             | native tooltip                                     |
| `onclick`  | `(event: MouseEvent) => void`                     | —             |                                                    |
| `children` | `Snippet`                                         | required      | label content                                      |

```svelte
<script lang="ts">
	import Button from '$lib/components/ui/Button.svelte';
	let saving = $state(false);
</script>

<Button variant="primary" loading={saving} onclick={() => (saving = true)}
	>Generate</Button
>
<Button variant="danger" size="sm" onclick={remove}>Delete</Button>
<Button variant="ghost" onclick={cancel}>Cancel</Button>
```

## Card

`import Card from '$lib/components/ui/Card.svelte';`

| Prop        | Type      | Default  | Notes                                                |
| ----------- | --------- | -------- | ---------------------------------------------------- |
| `hoverable` | `boolean` | `false`  | border lightens + `translateY(-1px)` on hover        |
| `title`     | `string`  | —        | renders default header row                           |
| `actions`   | `Snippet` | —        | right side of the default header row                 |
| `header`    | `Snippet` | —        | full custom header row (overrides `title`/`actions`) |
| `children`  | `Snippet` | required | body content                                         |
| `class`     | `string`  | `''`     | extra class on the root                              |

```svelte
<Card title="Renders" hoverable>
	{#snippet actions()}
		<Button size="sm" onclick={refresh}>Refresh</Button>
	{/snippet}
	<p>Body content here.</p>
</Card>
```

## StatusChip

`import StatusChip from '$lib/components/ui/StatusChip.svelte';`

| Prop     | Type     | Default  | Notes                                  |
| -------- | -------- | -------- | -------------------------------------- |
| `status` | `string` | required | matched case-insensitively after trim  |
| `label`  | `string` | —        | display text; defaults to raw `status` |

Status → color mapping (unknown strings fall back to muted and show the raw string):

| Tone    | Color var          | Statuses                                       |
| ------- | ------------------ | ---------------------------------------------- |
| success | `--success`        | `ready`, `done`, `completed`                   |
| info    | `--info`           | `running` (dot pulses), `generating`, `queued` |
| muted   | `--text-secondary` | `pending`, `idle`, `draft`, + any unknown      |
| error   | `--error`          | `failed`, `error`                              |
| warning | `--warning`        | `paused`                                       |
| accent  | `--accent`         | `in_progress`                                  |

```svelte
<StatusChip status={job.status} />
<StatusChip status="in_progress" label="Rendering" />
```

## ProgressBar

`import ProgressBar from '$lib/components/ui/ProgressBar.svelte';`

| Prop            | Type           | Default | Notes                                     |
| --------------- | -------------- | ------- | ----------------------------------------- |
| `value`         | `number`       | `0`     | clamped to 0–100                          |
| `indeterminate` | `boolean`      | `false` | sliding accent gradient, no % readout     |
| `label`         | `string`       | —       | text above the track, also the aria-label |
| `size`          | `'sm' \| 'md'` | `'md'`  | sm = 4px, md = 6px track                  |

```svelte
<ProgressBar value={job.progress} label={job.name} />
<ProgressBar indeterminate label="Connecting" size="sm" />
```

## Skeleton

`import Skeleton from '$lib/components/ui/Skeleton.svelte';`

| Prop     | Type      | Default  | Notes                                                   |
| -------- | --------- | -------- | ------------------------------------------------------- |
| `width`  | `string`  | `'100%'` | any CSS length                                          |
| `height` | `string`  | `'14px'` | any CSS length                                          |
| `circle` | `boolean` | `false`  | 50% radius; defaults width to `height` when width unset |

```svelte
<Skeleton width="60%" />
<Skeleton height="120px" />
<Skeleton circle height="32px" />
```

## Modal

`import Modal from '$lib/components/ui/Modal.svelte';`

| Prop          | Type         | Default  | Notes                                                                       |
| ------------- | ------------ | -------- | --------------------------------------------------------------------------- |
| `open`        | `boolean`    | `false`  | **bindable** — use `bind:open`                                              |
| `title`       | `string`     | —        | wired to `aria-labelledby`; falls back to `aria-label="Dialog"`             |
| `dismissible` | `boolean`    | `true`   | gates Esc, backdrop click, and the close button                             |
| `onclose`     | `() => void` | —        | fires once when the modal closes (any cause, incl. external `open = false`) |
| `footer`      | `Snippet`    | —        | right-aligned action row                                                    |
| `children`    | `Snippet`    | required | body content                                                                |

Behavior: Esc and backdrop click close when dismissible; focus is trapped while
open; initial focus goes to the first focusable element (or the panel); focus is
restored to the invoking element on close; body scroll is locked while open;
`role="dialog" aria-modal="true"`; backdrop blur + fade, panel rises 8px.

```svelte
<script lang="ts">
	import Modal from '$lib/components/ui/Modal.svelte';
	import Button from '$lib/components/ui/Button.svelte';
	let open = $state(false);
</script>

<Button variant="primary" onclick={() => (open = true)}>Open</Button>
<Modal bind:open title="Render settings" onclose={() => console.log('closed')}>
	<p>Body content.</p>
	{#snippet footer()}
		<Button variant="ghost" onclick={() => (open = false)}>Cancel</Button>
		<Button variant="primary" onclick={save}>Save</Button>
	{/snippet}
</Modal>
```

## ConfirmDialog

`import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';` (built on Modal)

| Prop           | Type         | Default     | Notes                                                               |
| -------------- | ------------ | ----------- | ------------------------------------------------------------------- |
| `open`         | `boolean`    | `false`     | **bindable** — use `bind:open`                                      |
| `title`        | `string`     | required    |                                                                     |
| `message`      | `string`     | required    | body copy                                                           |
| `confirmLabel` | `string`     | `'Confirm'` |                                                                     |
| `cancelLabel`  | `string`     | `'Cancel'`  |                                                                     |
| `danger`       | `boolean`    | `false`     | confirm button uses the danger variant                              |
| `onconfirm`    | `() => void` | required    | fires once, after the dialog closes                                 |
| `oncancel`     | `() => void` | —           | fires when closed without confirming (Esc, backdrop, cancel button) |

```svelte
<script lang="ts">
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	let confirmDelete = $state(false);
</script>

<ConfirmDialog
	bind:open={confirmDelete}
	title="Delete asset?"
	message="This removes the asset and its generated files. This cannot be undone."
	confirmLabel="Delete"
	danger
	onconfirm={deleteAsset}
	oncancel={() => console.log('kept')}
/>
```

## EmptyState

`import EmptyState from '$lib/components/ui/EmptyState.svelte';`

| Prop     | Type      | Default  | Notes                                  |
| -------- | --------- | -------- | -------------------------------------- |
| `title`  | `string`  | required |                                        |
| `body`   | `string`  | —        | supporting copy, max-width constrained |
| `icon`   | `Snippet` | —        | rendered muted above the title         |
| `action` | `Snippet` | —        | CTA row below the body                 |

```svelte
<EmptyState
	title="No assets yet"
	body="Create a character or location to start generating."
>
	{#snippet icon()}
		<Icon name="assets" size={28} />
	{/snippet}
	{#snippet action()}
		<Button variant="primary" onclick={addAsset}>New asset</Button>
	{/snippet}
</EmptyState>
```

## Spinner

`import Spinner from '$lib/components/ui/Spinner.svelte';`

| Prop   | Type                   | Default | Notes                           |
| ------ | ---------------------- | ------- | ------------------------------- |
| `size` | `'sm' \| 'md' \| 'lg'` | `'md'`  | sm = 14px, md = 18px, lg = 28px |

`role="status"` with an sr-only "Loading". Colors can be overridden from a parent
via the `--spinner-track` / `--spinner-arc` CSS vars (Button uses this on
primary/danger).

```svelte
<Spinner size="sm" />
```

## Icon

`import Icon from '$lib/components/ui/Icon.svelte';`

| Prop   | Type       | Default  | Notes                                                     |
| ------ | ---------- | -------- | --------------------------------------------------------- |
| `name` | `IconName` | required | union type from `icons.ts` — autocomplete lists all names |
| `size` | `number`   | `18`     | px, square                                                |

Stroke-based inline SVG (1.5px stroke, `currentColor`), inherits text color.
`aria-hidden` — when an icon is the only content of a control, put an
`aria-label` on the control itself. Use these instead of emoji everywhere.

Icon names (`import { icons, type IconName } from '$lib/components/ui/icons';`):

- Navigation: `home`, `story`, `assets`, `script`, `video`, `queue`, `settings`, `playground`
- Actions: `plus`, `trash`, `edit`, `retry`, `close`, `download`, `upload`, `search`, `external-link`, `zoom-in`
- Media transport: `play`, `pause`, `stop`
- Status: `check`, `alert`, `info`, `clock`, `sparkle`
- Media: `image`, `folder`, `film`, `music`
- Chevrons: `chevron-left`, `chevron-right`, `chevron-up`, `chevron-down`
- Misc: `drag`

```svelte
<script lang="ts">
	import Icon from '$lib/components/ui/Icon.svelte';
</script>

<Icon name="sparkle" />
<Icon name="play" size={24} />
<Button variant="ghost" title="Settings" aria-label="Settings"
	><Icon name="settings" /></Button
>
```
