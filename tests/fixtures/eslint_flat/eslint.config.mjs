export default [
  {
    rules: {
      'no-console': 'warn',
      'prefer-const': 'error',
    },
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
    },
  },
  {
    files: ['**/*.test.js'],
    rules: {
      'no-console': 'off',
    },
  },
];
