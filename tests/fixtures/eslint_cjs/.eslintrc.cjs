module.exports = {
  root: true,
  env: { node: true, es6: true },
  parserOptions: { ecmaVersion: 'latest', sourceType: 'module' },
  extends: ['eslint:recommended'],
  plugins: ['check-file'],
  rules: {
    'no-console': 'warn',
    'no-debugger': 'error',
  },
};
