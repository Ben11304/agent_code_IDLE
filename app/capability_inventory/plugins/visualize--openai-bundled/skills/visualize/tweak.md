# Tweakable UI mockups

The app supplies a small `Tweak` object-binding helper. It sends controls to the host and applies returned values to your bound objects; it does not render the Tweak.js panel. Do not import Tweak.js or generate host API wiring, callback IDs, DOM event listeners, theme synchronization, or a controls launcher.

Render the mock's initial state first. Guard the optional helper so the same HTML still works in hosts and standalone renderers without design controls:

```js
const state = { radius: 18, accent: "#7c3aed", playing: true };
const player = document.getElementById("music-player");

function render() {
  player.style.borderRadius = `${state.radius}px`;
  player.style.setProperty("--player-accent", state.accent);
  player.querySelector("button").textContent = state.playing ? "Pause" : "Play";
}
render();

if (globalThis.Tweak) {
  const tweak = new Tweak({ container: player, onChange: render });
  tweak.addSlider(state, "radius", { label: "Corner radius", min: 0, max: 40, unit: "px" });
  tweak.addColorPicker(state, "accent", { label: "Accent", reference: "--player-accent" });
  tweak.addToggle(state, "playing", { label: "Playing" });
}
```

Give each component a descriptive `aria-label` for its group heading. Use a separate `Tweak` instance for each independently editable element; the host combines the groups in one panel. Keep the registered element alive and update its styles or descendants in `onChange` rather than replacing it.

- `addSlider(object, property, { min, max, step = 1, unit?, label?, reference? })` binds a number. `unit` is display context in the label, not part of the numeric value.
- `addColorPicker(object, property, { label?, reference? })` binds a hex color string.
- `addToggle(object, property, { label?, reference? })` binds a boolean.
- `addSelect(object, property, { options, label?, reference? })` binds a string. Options can be strings or `{ label, value }` objects.

The helper updates the bound property before calling `onChange`, including reset and temporary original preview. Make `onChange` a deterministic local render function, not a network write or irreversible action. Initial values are the current mock state. Use at most 12 controls per component and 12 options per select. Optional `reference` hints identify a token or state property, for example `--player-accent` or `player.radius`; use no spaces or punctuation other than `_ - . / : @ $ #`.

The host owns opening, closing, reset, and submitting changes. Missing annotation support is inert (`tweak.supported` is false). Page cleanup is automatic; call `tweak.dispose()` only if removing the component before navigation.
