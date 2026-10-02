# WebMCP tools for new sites

Use this reference only when the main Sites skill determines that WebMCP applies.

WebMCP is a proposed browser standard that lets websites expose their functionality directly to AI agents as structured tools.

It exposes declarative and imperative APIs, and the full specification can be found at https://webmachinelearning.github.io/webmcp/

The ChatGPT implementation supports only the imperative interface.

## API structure

```ts
interface Document {
  readonly modelContext?: ModelContext; // Page-scoped registry; feature-detect support.
}

interface ModelContext {
  registerTool(
    tool: {
      name: string; // Unique, stable action identifier.
      description: string; // What the tool does and when to use it.
      inputSchema: object; // JSON Schema describing accepted input.
      execute(input: unknown): unknown | Promise<unknown>; // Validate, act, return result.
      title?: string; // Human-readable display label.
      annotations?: {
        // Behavioral hints, not security boundaries.
        readOnlyHint?: boolean; // True only when no state changes.
        untrustedContentHint?: boolean; // True for external or user-generated output.
      };
    },
    options?: {
      signal?: AbortSignal; // Abort to unregister this tool.
    },
  ): void | Promise<void>; // Register one tool for this page.
}
```

## Example call site

```ts
const context =
  typeof document === "undefined" ? undefined : document.modelContext;
if (!context?.registerTool) return;
const lifecycle = new AbortController();

try {
  void Promise.resolve(
    context.registerTool(
      {
        name: "create_booking",
        title: "Create booking",
        description:
          "Book the selected slot and update the visible reservation.",
        inputSchema: {
          type: "object",
          properties: { slotId: { type: "string" } },
          required: ["slotId"],
          additionalProperties: false,
        },
        annotations: { readOnlyHint: false, untrustedContentHint: false },
        async execute(input) {
          const booking = await bookSlot(validateInput(input));
          return { id: booking.id, status: "confirmed" };
        },
      },
      { signal: lifecycle.signal },
    ),
  ).catch(reportError);
} catch (error) {
  reportError(error);
}

return () => lifecycle.abort();
```

## Tool-design rules

- Use the same state and actions as the visible interface; the tools are another way to complete the site's real primary journeys.
- Distinguish **read**, **navigate/start**, **stage/configure**, and **complete** in names and descriptions. `create_event` means an event is created; `start_event_creation` only opens or prepares a flow. Never conceal side effects in an ambiguous verb.
- Prefer batched APIs where possible, rather than requiring clients to write loops.
- Return concise JSON-serializable results only after the action and visible state finish updating.
- Register once client-side, clean up with `AbortSignal`, and handle unsupported browsers and registration failures.
- Do not add speculative tools.

## Validate before hosting

Before `sites-hosting`, use a supported WebMCP context to verify every intended tool:

- it registers with the expected name, schema, and annotations;
- a representative valid input updates the same app state as the visible interface and returns the expected concise result; and
- an invalid input or expected failure path fails intentionally without corrupting state.

Use a read-back tool or equivalent app-state check when needed to verify the state transition. This focused WebMCP contract check is required even when broader browser UI QA was not requested; do not substitute source inspection, a successful build, DOM inspection, or a click fallback for it. If no supported WebMCP validation context is available, do not claim the tools were verified. Treat this as a blocker only when the user explicitly requested WebMCP support; otherwise continue to hosting with the validation gap recorded.
