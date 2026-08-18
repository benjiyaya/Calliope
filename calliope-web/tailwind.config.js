module.exports = {
	content: ['./src/**/*.{html,js,svelte,ts}'],
	theme: {
		extend: {
			colors: {
				primary: '#0a0a0c',
				surface: '#141418',
				elevated: '#1e1e24',
				border: '#2a2a32',
				muted: '#71717a',
				accent: '#8b5cf6',
				'accent-hover': '#7c3aed',
			},
			borderRadius: {
				sm: '6px',
				md: '10px',
				lg: '16px',
			},
		},
	},
	plugins: [],
};
