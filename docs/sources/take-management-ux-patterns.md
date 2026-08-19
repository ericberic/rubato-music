# Take-management UX patterns

Reviewed 2026-07-19 from official product documentation to inform Rubato's
passage-local repeated-take workflow.

## Sources

- Apple Logic Pro, [Preview take recordings in Logic Pro for
  Mac](https://support.apple.com/guide/logicpro/preview-take-recordings-lgcp317d76de/mac):
  a closed take folder presents one active take and a pop-up menu selects the
  alternate recording; the alternatives do not all occupy the main timeline.
- Apple Logic Pro for iPad, [Record multiple takes and create comps in Logic
  Pro for iPad](https://support.apple.com/en-ie/guide/logicpro-ipad/lpip458ce486/ipados):
  repeated recordings are collected as takes for one region and expanded only
  when the musician wants to edit or combine them.
- Ableton Live, [Comping](https://www.ableton.com/en/live-manual/11/comping/)
  and [Comping in Live
  FAQ](https://help.ableton.com/hc/en-us/articles/360019092580-Comping-in-Live-FAQ):
  take lanes are hidden by default and revealed for focused editing, keeping
  the ordinary arrangement compact.
- Apple Voice Memos, [Edit or delete a recording in Voice Memos on
  iPhone](https://support.apple.com/en-ie/guide/iphone/iphc9bdaee83/ios):
  editing and deletion live behind secondary controls, and deletion is
  recoverable through Recently Deleted.
- Apple, [Human Interface Guidelines: Icons](https://developer.apple.com/design/human-interface-guidelines/icons)
  and [Toolbars](https://developer.apple.com/design/human-interface-guidelines/toolbars):
  use streamlined familiar symbols, standard placement, and text alternatives
  rather than inventing a dense private icon language.
- W3C WAI-ARIA Authoring Practices, [Button
  pattern](https://www.w3.org/WAI/ARIA/apg/patterns/button/) and [Accessible
  names](https://www.w3.org/WAI/ARIA/apg/practices/names-and-descriptions/):
  icon-only buttons require concise programmatic names; visible labels remain
  preferable when the action is unfamiliar.

## Rubato implications

Rubato's primary organizing coordinate is a score passage, not a DAW timeline
region, but the disclosure pattern transfers directly:

1. Show one selected recording in the passage workspace and choose alternates
   through a compact picker.
2. Use a one-line recording picker instead of expanding every full card. A
   small, familiar icon toolbar can expose the selected pass's common playback
   and score-location actions; every icon needs an accessible name and tooltip.
3. Put record/re-record and Yamaha review in the primary layer; browser
   diagnostics, exclude, and dismiss belong in secondary recording options.
   Raw MIDI persists automatically in the workspace, so download is developer
   plumbing rather than a routine rehearsal action.
4. Keep curation recoverable and say so. A failed zero-note attempt is an
   incomplete recording, not a score-alignment verdict.

Rubato deliberately does not import DAW comping wholesale. Kept passes remain
separate symbolic observations for learning; they are not sliced and combined
into a composite audio performance.
