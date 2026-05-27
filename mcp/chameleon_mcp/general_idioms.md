1. Search the codebase for an existing utility, helper, or service before creating a new one.
2. Match the testing granularity of sibling files - if similar classes have no test file, don't create one.
3. Use the language's built-in finder/upsert idiom (find_or_initialize_by, Map.get, ??=) instead of manual check-then-create.
4. Keep each class at the same abstraction level as its neighbors - don't over-extract small operations into separate files when siblings inline them.
5. Separate data queries from side-effect operations - don't add file downloads to a JSON endpoint.
6. Follow the codebase's existing pattern for resource lookup parameters (query params vs path segments vs body).
7. Check if the codebase already wraps a library before importing it directly (custom HTTP client, logger, query hook).
8. Inherit the same base class or mixin that sibling files use unless the new file has a fundamentally different responsibility.
9. Mirror the error handling pattern of neighboring files - if they use render_data/render_error, don't use raw render json:.
10. Use the codebase's DSL or macro for a pattern (scopes, validations, hooks) instead of reimplementing in plain code.
