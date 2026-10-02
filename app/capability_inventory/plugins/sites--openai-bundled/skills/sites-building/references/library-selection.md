# Library selection

While planning the initial implementation, map the product capabilities to the relevant rows below. Use judgment to decide which capabilities the product needs. Once you include a capability covered here, default to the mapped implementation unless an explicit user requirement or a compatible library already established by an existing project takes precedence. This is an opinionated set of defaults for common capabilities, not an exhaustive library catalog.

> **Do not treat this reference as a checklist.** Start from the requested product and use only rows that correspond to necessary behavior. Do not add features, dependencies, abstractions, or layers merely because they appear here, and do not distort a straightforward product design to make it fit a listed library. For unlisted capabilities, choose a specialized library case by case or write focused product code.

Commands use npm syntax. Check `package.json` first and reuse suitable declared versions. Use the project's existing package manager for missing packages or required version changes, and keep its lockfile in sync.

## Interface foundations

| For this | Use this | Project source or install command |
| --- | --- | --- |
| Controls, forms, overlays, navigation, feedback, and layout primitives | The matching shadcn component in `components/ui` | `components/ui/*.tsx`; preinstalled, no command |
| Interface icons | Lucide | `lucide-react` is preinstalled; no command |
| Command menus and comboboxes | The shadcn Command or Combobox component backed by cmdk | `components/ui/command.tsx` and `components/ui/combobox.tsx`; preinstalled, no command |
| Carousels | The shadcn Carousel component backed by Embla | `components/ui/carousel.tsx`; preinstalled, no command |
| Resizable panel layouts | The shadcn Resizable component | `components/ui/resizable.tsx`; preinstalled, no command |

Select matching installed primitives directly using the semantic map in `sites-building`; do not inventory the catalog or inspect implementations proactively. Open only the selected component when its exports or props are unclear for the next edit. Import through the project’s existing component path (`@/components/ui/...` in the standard starter). Use semantic HTML for content and layout that does not need an interactive primitive.

Compose the installed primitives rather than recreating them. Customize through their existing props, variants, and call-site `className` values while preserving accessibility and interaction behavior.

## Forms and structured input

| For this | Use this | Project source or install command |
| --- | --- | --- |
| A small form with straightforward state | React state or native form handling with shadcn Field, Input, Select, Checkbox, and related controls | `components/ui/field.tsx`, `input.tsx`, `select.tsx`, `checkbox.tsx`, and related files; preinstalled, no command |
| A complex form with many fields, validation, or dynamic sections | React Hook Form with Zod schemas and the Hook Form resolver | Reuse existing `react-hook-form`, `zod`, and `@hookform/resolvers`; add only missing packages |
| Schema validation without complex form state | Zod | Reuse existing `zod`; add only if missing |
| Date selection and date formatting | The shadcn Calendar component with React DayPicker and date-fns | `components/ui/calendar.tsx`; `react-day-picker` and `date-fns` are preinstalled, no command |

## Data and analytical interfaces

| For this | Use this | Project source or install command |
| --- | --- | --- |
| A small, mostly presentational table | Semantic table markup with the shadcn Table component | `components/ui/table.tsx`; preinstalled, no command |
| Sorting, filtering, pagination, grouping, or column state | TanStack Table rendered with the shadcn Table component | `npm install @tanstack/react-table` |
| A very large list or table that must render only visible rows | TanStack Virtual | `npm install @tanstack/react-virtual` |
| Quantitative charts and trends, including line, area, bar, pie, radar, radial, and sparkline views | Recharts through the shadcn Chart component | `components/ui/chart.tsx`; `recharts` is preinstalled, no command |
| Single-value progress, budget, or usage meters | The shadcn Progress component | `components/ui/progress.tsx`; preinstalled, no command |

## Client data and application state

| For this | Use this | Project source or install command |
| --- | --- | --- |
| Data needed during server rendering | Server Components and `fetch` | Built in; no command |
| Client-side caching, refetching, mutations, or optimistic server state | TanStack Query | `npm install @tanstack/react-query` |
| State local to one component or feature | React state, context, or a reducer | Built in; no command |
| Shared client state spanning unrelated features | Zustand | `npm install zustand` |

Add only the libraries required by the selected rows. Do not install overlapping libraries for the same capability.
