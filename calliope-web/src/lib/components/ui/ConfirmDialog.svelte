<script lang="ts">
	import Button from './Button.svelte';
	import Modal from './Modal.svelte';

	interface Props {
		open?: boolean;
		title: string;
		message: string;
		confirmLabel?: string;
		cancelLabel?: string;
		danger?: boolean;
		onconfirm: () => void;
		oncancel?: () => void;
	}

	let {
		open = $bindable(false),
		title,
		message,
		confirmLabel = 'Confirm',
		cancelLabel = 'Cancel',
		danger = false,
		onconfirm,
		oncancel,
	}: Props = $props();

	// Set when the user confirms, so the ensuing close is not reported as a cancel.
	let confirmed = false;

	function handleConfirm() {
		confirmed = true;
		open = false;
		onconfirm();
	}

	function handleClose() {
		if (confirmed) {
			confirmed = false;
			return;
		}
		oncancel?.();
	}
</script>

<Modal bind:open {title} onclose={handleClose}>
	<p class="confirm-message">{message}</p>
	{#snippet footer()}
		<Button variant="ghost" onclick={() => (open = false)}>{cancelLabel}</Button
		>
		<Button variant={danger ? 'danger' : 'primary'} onclick={handleConfirm}
			>{confirmLabel}</Button
		>
	{/snippet}
</Modal>

<style>
	.confirm-message {
		margin: 0;
		font-size: 14px;
		line-height: 1.5;
		color: var(--text-secondary);
	}
</style>
