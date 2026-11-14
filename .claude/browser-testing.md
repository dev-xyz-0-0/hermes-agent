
# Browser Testing Rules

## Primary Browser Automation

- Always prefer Playwright MCP for browser testing.
- Do not use Python CDPSession when Playwright MCP is available.
- Use browser_navigate to open webpages.
- Use browser_snapshot to inspect page elements.
- Use browser_click and browser_type for UI interactions.
- Use browser_take_screenshot for visual evidence.
- Use browser_console_messages for JavaScript errors.
- Use browser_network_requests for API failures.
- Use browser_resize for responsive testing.

## Testing Workflow

1. Verify Playwright MCP connectivity.
2. Navigate to the target URL.
3. Inspect the accessibility snapshot.
4. Execute functional browser tests.
5. Check console and network errors.
6. Capture screenshots of failures.
7. Report PASS / FAIL / BLOCKED results.

## Additional Tools

- CodeGraph MCP: Source-code dependency and impact analysis.
- Chrome DevTools MCP: Advanced browser debugging.
- Playwright Test: Repeatable CI/CD regression tests.

## Restrictions

- Do not silently fall back to custom CDP scripts.
- Do not claim tests passed without execution.
- Do not modify application code unless authorized.
