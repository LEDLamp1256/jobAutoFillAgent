# Local application fixture

Open `multi_step_application.html` locally in a browser during later browser-stage work. It has four semantic steps: basic information, conditional employment, repeated email, and final review. `Continue` is `type="button"`; only the final control is `type="submit"`. Choosing `Yes` reveals a required employer field. A failed validation leaves the visible step and progress text unchanged and displays a `role="alert"` message. No external assets, network calls, randomness, or timers are used.

The V2-0 tests inspect fixture structure only. They do not claim its browser behavior has been exercised; that belongs to the later browser-adapter stage.
