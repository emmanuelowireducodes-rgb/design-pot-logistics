# Design-Pot visual system

A calm operational interface: warm neutral canvas, white surfaces, forest green actions, muted borders and restrained semantic color. System typography avoids external font requests. 8px spacing rhythm, 7px controls, 14px cards; shadows are subtle.

Tokens and components live in `static/app.css`. Buttons use primary/secondary variants; inputs have visible labels, 44px minimum height and focus rings. Status badges always include text. Tables have captions and scoped headers; smaller screens allow horizontal scrolling on client/admin lists, while field deliveries become cards.

Reusable templates: delivery table, pagination, empty state, skeleton. Alerts cover success/error/offline. Server-rendered pages do not simulate skeleton delays; native page navigation is immediate, and form submissions display a busy state. Skeletons are available for later async components.

Navigation collapses on narrow screens, supports Escape and exposes expanded state. Field actions use 48px targets. Reduced-motion preferences disable animations. The skip link moves focus to the main content. Modal/sheet patterns use native dialog focus management; tab patterns should use links for page navigation and buttons only for actual panels.

The gallery at `/admin/components/` demonstrates controls, cards, badges, tabs, alerts, loading, empty state, native modal and mobile action bar. It is a staff-only development reference, not part of the operational sidebar.
