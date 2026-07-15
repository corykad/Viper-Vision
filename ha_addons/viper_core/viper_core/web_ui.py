from html import escape
from urllib.parse import quote


CHIME_EVENTS = [
    ("front_doorbell", "Front Doorbell"),
    ("back_doorbell", "Back Doorbell"),
    ("fridge_open", "Fridge Open"),
    ("fridge_closed", "Fridge Closed"),
    ("freezer_open", "Freezer Open"),
    ("freezer_closed", "Freezer Closed"),
]

PAGES = [
    ("dashboard", "Dashboard"),
    ("doorbells", "Doorbells"),
    ("speakers", "Speakers"),
    ("chimes", "Chimes"),
    ("refrigerator", "Refrigerator"),
    ("heat-pumps", "Heat Pumps"),
    ("vacuum", "Vacuum"),
    ("settings", "Settings"),
    ("diagnostics", "Diagnostics"),
]


def render_page(state, page="dashboard"):
    control = state.get("control") or {}
    speakers = control.get("speakers") or {}
    chimes = control.get("chimes") or {}
    settings = control.get("settings") or {}
    devices = state.get("devices") or {}
    available_chimes = chimes.get("available") or []
    selected_chimes = chimes.get("events") or {}
    recent_events = state.get("recent_events") or []
    health = "Healthy" if state.get("ok") else "Needs attention"
    page = _normalize_page(page)
    sections = {
        "dashboard": [
            _section("System", [
                f"<p><strong>Status:</strong> {_e(health)}</p>",
                f"<p><strong>Home Assistant:</strong> {_e((state.get('home_assistant') or {}).get('message', 'unknown'))}</p>",
                f"<p><strong>Dependencies:</strong> {_e((state.get('dependencies') or {}).get('message', 'unknown'))}</p>",
                '<div class="actions">'
                + _post_button("ui/control/armed", "state", "true", "Arm Viper")
                + _post_button("ui/control/armed", "state", "false", "Disarm Viper")
                + _post_button("ui/control/global_mute", "state", "true", "Mute All")
                + _post_button("ui/control/global_mute", "state", "false", "Unmute All")
                + "</div>",
            ]),
            _section("At A Glance", [_dashboard_summary(state)]),
            _section("Recent Events", [_recent_events(recent_events)]),
        ],
        "doorbells": [
            _section("Doorbell Status", [_doorbell_summary(settings)]),
            _section("Doorbell Tests", [_doorbell_test_buttons()]),
            _section("Doorbell AI", [_doorbell_ai_form(settings)]),
        ],
        "speakers": [
            _section("Speakers", [_speaker_summary(speakers), _speaker_table(speakers), _add_speaker_form()]),
        ],
        "chimes": [
            _section("Chime Assignments", [_chime_assignments(selected_chimes, available_chimes)]),
            _section("Manage Chime Files", [_manage_chime_files(available_chimes)]),
        ],
        "refrigerator": [
            _section("Refrigerator Status", [_refrigerator_status(devices.get("refrigerator") or {})]),
            _section("Refrigerator Tests", [_fridge_test_buttons()]),
        ],
        "heat-pumps": [
            _section("Heat Pumps", [_heat_pump_status(devices.get("heat_pumps") or []), _airflow_status(devices.get("airflow") or []), _hvac_form()]),
        ],
        "vacuum": [
            _section("Vacuum", [_vacuum_status(devices.get("vacuum") or {}, devices.get("vacuum_status") or {}), _vacuum_form()]),
        ],
        "settings": [
            _section("Settings", [_settings_form(settings)]),
            _section("Broadcast", [_broadcast_form()]),
        ],
        "diagnostics": [
            _section("Diagnostics", [_diagnostics(state)]),
            _section("Tests", [_utility_test_buttons()]),
        ],
    }
    title = dict(PAGES).get(page, "Dashboard")
    return _page_shell("".join(sections.get(page) or sections["dashboard"]), page, title)


def render_all_page_for_legacy_tests(state):
    control = state.get("control") or {}
    speakers = control.get("speakers") or {}
    chimes = control.get("chimes") or {}
    settings = control.get("settings") or {}
    devices = state.get("devices") or {}
    available_chimes = chimes.get("available") or []
    selected_chimes = chimes.get("events") or {}
    recent_events = state.get("recent_events") or []
    health = "Healthy" if state.get("ok") else "Needs attention"
    rows = [_section("System", [
        f"<p><strong>Status:</strong> {_e(health)}</p>",
        f"<p><strong>Home Assistant:</strong> {_e((state.get('home_assistant') or {}).get('message', 'unknown'))}</p>",
        f"<p><strong>Dependencies:</strong> {_e((state.get('dependencies') or {}).get('message', 'unknown'))}</p>",
        '<div class="actions">'
        + _post_button("ui/control/armed", "state", "true", "Arm Viper")
        + _post_button("ui/control/armed", "state", "false", "Disarm Viper")
        + _post_button("ui/control/global_mute", "state", "true", "Mute All")
        + _post_button("ui/control/global_mute", "state", "false", "Unmute All")
        + "</div>",
    ])]
    rows.append(_section("Speakers", [
        _speaker_table(speakers),
        _add_speaker_form(),
    ]))
    rows.append(_section("Chimes", [
        _chime_assignments(selected_chimes, available_chimes),
        _manage_chime_files(available_chimes),
    ]))
    rows.append(_section("Settings", [_settings_form(settings)]))
    rows.append(_section("Doorbell AI", [_doorbell_ai_form(settings), _doorbell_test_buttons()]))
    rows.append(_section("Heat Pumps", [_heat_pump_status(devices.get("heat_pumps") or []), _airflow_status(devices.get("airflow") or []), _hvac_form()]))
    rows.append(_section("Vacuum", [_vacuum_status(devices.get("vacuum") or {}, devices.get("vacuum_status") or {}), _vacuum_form()]))
    rows.append(_section("Refrigerator", [_refrigerator_status(devices.get("refrigerator") or {})]))
    rows.append(_section("Diagnostics", [_diagnostics(state)]))
    rows.append(_section("Tests", [
        "<h3>Doorbells</h3>",
        _doorbell_test_buttons(),
        "<h3>Refrigerator</h3>",
        _fridge_test_buttons(),
        "<h3>Utilities</h3>",
        _utility_test_buttons(),
        _broadcast_form(),
    ]))
    rows.append(_section("Recent Events", [_recent_events(recent_events)]))
    return _page_shell("\n".join(rows), "dashboard", "All Controls")


def _page_shell(body, current_page, title):
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Viper Core</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; background: #f6f7f8; color: #171717; }}
    header {{ background: #102033; color: white; padding: 18px 22px; }}
    nav {{ background: #ffffff; border-bottom: 1px solid #d8dde3; padding: 10px 18px; }}
    nav a {{ display: inline-block; margin: 4px 8px 4px 0; padding: 8px 10px; border: 1px solid #cbd3dc; border-radius: 4px; color: #102033; text-decoration: none; }}
    nav a[aria-current="page"] {{ background: #102033; color: white; border-color: #102033; }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 18px; }}
    section {{ background: white; border: 1px solid #d8dde3; border-radius: 6px; margin: 0 0 16px; padding: 16px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border-bottom: 1px solid #e5e8ec; padding: 8px; text-align: left; vertical-align: top; }}
    label {{ display: block; font-weight: 650; margin: 10px 0 4px; }}
    input, select, textarea {{ box-sizing: border-box; width: 100%; max-width: 560px; padding: 8px; }}
    button {{ margin: 4px 6px 4px 0; padding: 8px 12px; font-weight: 650; }}
    .actions form {{ display: inline; }}
    .compact form {{ display: inline; }}
    .summary {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .summary p {{ border: 1px solid #e5e8ec; border-radius: 6px; margin: 0; padding: 10px; }}
    .status-line {{ margin: 10px 0; font-weight: 650; }}
    .muted {{ color: #5b6470; }}
  </style>
</head>
<body>
<header><h1>Viper Core</h1><p>Home Assistant-hosted controls for Viper.</p></header>
{_nav(current_page)}
<main><h2>{_e(title)}</h2>{body}</main>
<script>
function viperBasePath() {{
  const marker = '/api/hassio_ingress/';
  const path = window.location.pathname || '/';
  const index = path.indexOf(marker);
  if (index >= 0) {{
    const rest = path.slice(index + marker.length);
    const token = rest.split('/')[0] || '';
    return path.slice(0, index) + marker + token + '/';
  }}
  return path.endsWith('/') ? path : path.replace(/[^/]*$/, '');
}}
function viperUrl(path) {{
  let target = String(path || '');
  try {{
    const parsed = new URL(target, window.location.href);
    target = parsed.pathname + parsed.search;
  }} catch (_error) {{
  }}
  const uiIndex = target.indexOf('/ui/');
  if (uiIndex >= 0) {{
    target = target.slice(uiIndex + 1);
  }}
  if (target.startsWith('/')) {{
    target = target.slice(1);
  }}
  return viperBasePath() + target;
}}
async function postChimeTest(payload, status, button) {{
  if (status) {{
    status.textContent = 'Running test...';
  }}
  if (button) {{
    button.disabled = true;
  }}
  try {{
    const response = await fetch(viperUrl('ui/chimes/test'), {{
      method: 'POST',
      body: payload.toString(),
      headers: {{
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Viper-Async': 'true'
      }}
    }});
    const result = await response.json();
    if (status) {{
      status.textContent = result.message || (result.ok ? 'Test sent.' : 'Test failed.');
    }}
  }} catch (error) {{
    if (status) {{
      status.textContent = 'Test failed: ' + error;
    }}
  }} finally {{
    if (button) {{
      button.disabled = false;
    }}
  }}
}}
document.addEventListener('submit', async function (event) {{
  const form = event.target;
  const submitter = event.submitter;
  const isAsync = form.matches('form[data-async="true"]') || (submitter && submitter.matches('[data-async="true"]'));
  if (!isAsync) {{
    return;
  }}
  event.preventDefault();
  const statusId = (submitter && submitter.getAttribute('data-status-target')) || form.getAttribute('data-status-target') || 'async_status';
  const status = document.getElementById(statusId);
  const button = submitter || form.querySelector('button');
  if (status) {{
    status.textContent = 'Running test...';
  }}
  if (button) {{
    button.disabled = true;
  }}
  try {{
    const payload = new URLSearchParams();
    for (const element of form.elements) {{
      if (!element.name || element.disabled) {{
        continue;
      }}
      const type = (element.type || '').toLowerCase();
      if ((type === 'checkbox' || type === 'radio') && !element.checked) {{
        continue;
      }}
      payload.append(element.name, element.value || '');
    }}
    const response = await fetch(viperUrl((submitter && submitter.formAction) || form.getAttribute('action') || form.action), {{
      method: (submitter && submitter.formMethod) || form.method || 'POST',
      body: payload.toString(),
      headers: {{
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Viper-Async': 'true'
      }}
    }});
    const result = await response.json();
    if (status) {{
      status.textContent = result.message || (result.ok ? 'Test sent.' : 'Test failed.');
    }}
  }} catch (error) {{
    if (status) {{
      status.textContent = 'Test failed: ' + error;
    }}
  }} finally {{
    if (button) {{
      button.disabled = false;
    }}
  }}
}});
document.addEventListener('click', async function (event) {{
  const button = event.target.closest('button[data-test-selected-chime="true"]');
  if (!button) {{
    return;
  }}
  event.preventDefault();
  const form = button.closest('form');
  const status = document.getElementById(button.getAttribute('data-status-target') || 'chime_assignment_status');
  if (!form) {{
    if (status) {{
      status.textContent = 'Test failed: chime form not found.';
    }}
    return;
  }}
  const filename = form.querySelector('select[name="filename"]');
  const eventName = form.querySelector('input[name="event"]');
  const payload = new URLSearchParams();
  payload.append('filename', filename ? filename.value : '');
  payload.append('event', eventName ? eventName.value : '');
  await postChimeTest(payload, status, button);
}});
document.addEventListener('click', async function (event) {{
  const button = event.target.closest('button[data-test-file-chime="true"]');
  if (!button) {{
    return;
  }}
  event.preventDefault();
  const status = document.getElementById(button.getAttribute('data-status-target') || 'chime_test_status');
  const payload = new URLSearchParams();
  payload.append('filename', button.getAttribute('data-filename') || '');
  payload.append('event', button.getAttribute('data-event') || '');
  await postChimeTest(payload, status, button);
}});
</script>
</body>
</html>"""


def _nav(current_page):
    links = []
    for page, label in PAGES:
        current = ' aria-current="page"' if page == current_page else ""
        links.append(f'<a href="?page={_e(page)}"{current}>{_e(label)}</a>')
    return f"<nav aria-label=\"Viper Core pages\">{''.join(links)}</nav>"


def _section(title, parts):
    return f"<section><h2>{_e(title)}</h2>{''.join(parts)}</section>"


def _normalize_page(page):
    page = str(page or "dashboard").strip().lower()
    known = {item[0] for item in PAGES}
    return page if page in known else "dashboard"


def _dashboard_summary(state):
    control = state.get("control") or {}
    devices = state.get("devices") or {}
    settings = control.get("settings") or {}
    speakers = control.get("speakers") or {}
    enabled_speakers = sum(1 for speaker in speakers.values() if speaker.get("enabled", True))
    heat_pumps = devices.get("heat_pumps") or []
    heat_pump_issues = sum(1 for unit in heat_pumps if not unit.get("ok"))
    refrigerator = devices.get("refrigerator") or {}
    fridge_issues = sum(1 for item in refrigerator.values() if item and not item.get("ok"))
    return (
        '<div class="summary">'
        f"<p><strong>Viper:</strong> {'armed' if control.get('armed', True) else 'disarmed'}; {'muted' if control.get('global_mute') else 'audio on'}.</p>"
        f"<p><strong>Speakers:</strong> {_e(enabled_speakers)} enabled.</p>"
        f"<p><strong>Doorbells:</strong> {_e(settings.get('doorbell_video_mode', 'fast'))} video follow-up; Gemini {'ready' if settings.get('gemini_configured') else 'not configured'}.</p>"
        f"<p><strong>Heat pumps:</strong> {_e(len(heat_pumps))} units; {_e(heat_pump_issues)} need attention.</p>"
        f"<p><strong>Refrigerator:</strong> {_e(fridge_issues)} items need attention.</p>"
        f"<p><strong>Vacuum:</strong> {_e((devices.get('vacuum') or {}).get('state', 'unknown'))}.</p>"
        "</div>"
    )


def _doorbell_summary(settings):
    mode = settings.get("doorbell_video_mode", "fast")
    front_stream = "configured" if settings.get("front_door_stream_url") else "not configured"
    back_stream = "configured" if settings.get("back_door_stream_url") else "not configured"
    return (
        '<div class="summary">'
        f"<p><strong>Automatic Video Follow-Up:</strong> {_e(mode)}</p>"
        f"<p><strong>Front Snapshot:</strong> {_e(settings.get('front_door_camera_entity', 'camera.front_door_snapshot'))}</p>"
        f"<p><strong>Back Snapshot:</strong> {_e(settings.get('back_door_camera_entity', 'camera.back_door_snapshot'))}</p>"
        f"<p><strong>Live Video:</strong> front {_e(front_stream)}, back {_e(back_stream)}.</p>"
        "</div>"
    )


def _speaker_summary(speakers):
    enabled = sum(1 for speaker in speakers.values() if speaker.get("enabled", True))
    doorbell = sum(1 for speaker in speakers.values() if speaker.get("enabled", True) and speaker.get("doorbell", True))
    fridge = sum(1 for speaker in speakers.values() if speaker.get("enabled", True) and speaker.get("fridge", True))
    utilities = sum(1 for speaker in speakers.values() if speaker.get("enabled", True) and speaker.get("utilities", True))
    return (
        '<div class="summary">'
        f"<p><strong>Enabled:</strong> {_e(enabled)} speakers.</p>"
        f"<p><strong>Doorbell Route:</strong> {_e(doorbell)} speakers.</p>"
        f"<p><strong>Refrigerator Route:</strong> {_e(fridge)} speakers.</p>"
        f"<p><strong>Utility Route:</strong> {_e(utilities)} speakers.</p>"
        "</div>"
    )


def _speaker_table(speakers):
    rows = [
        "<table><thead><tr><th>Speaker</th><th>Target</th><th>Routes</th><th>Actions</th></tr></thead><tbody>"
    ]
    for name, speaker in sorted(speakers.items()):
        enabled = bool(speaker.get("enabled", True))
        routes = []
        for route in ("doorbell", "fridge", "utilities"):
            route_enabled = bool(speaker.get(route, True))
            routes.append(
                _post_button(
                    f"ui/speakers/{quote(name)}/route",
                    "route_state",
                    f"{route}:{str(not route_enabled).lower()}",
                    f"Turn {route.title()} {'Off' if route_enabled else 'On'}",
                )
            )
        actions = (
            _post_button(f"ui/speakers/{quote(name)}/enabled", "state", str(not enabled).lower(), "Disable" if enabled else "Enable")
            + _post_button(f"ui/speakers/{quote(name)}/delete", "", "", "Delete")
        )
        rows.append(
            "<tr>"
            f"<td>{_e(_display_name(name))}<br><span class='muted'>{'enabled' if enabled else 'disabled'}</span></td>"
            f"<td>{_e(speaker.get('type', ''))}: {_e(speaker.get('id', ''))}</td>"
            f"<td class='compact'>{''.join(routes)}</td>"
            f"<td class='compact'>{actions}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _add_speaker_form():
    return """<form action="ui/speakers" method="post">
<h3>Add Or Update Speaker</h3>
<label for="speaker_name">Name</label><input id="speaker_name" name="name">
<label for="speaker_id">Entity ID Or IP Address</label><input id="speaker_id" name="id">
<label for="speaker_type">Type</label><select id="speaker_type" name="type"><option value="ha">Home Assistant Speaker</option><option value="alexa">Alexa Media Player</option><option value="sonos">Direct Sonos IP</option></select>
<button type="submit">Save Speaker</button>
</form>"""


def _doorbell_ai_form(settings):
    mode = settings.get("doorbell_video_mode", "fast")
    mode_options = []
    for value, label in [
        ("fast", "Fast: still image only"),
        ("smart", "Smart: live follow-up only when still image is weak"),
        ("detailed", "Detailed: live follow-up after every alert"),
        ("manual", "Manual: only when a button is pressed"),
    ]:
        selected = " selected" if value == mode else ""
        mode_options.append(f'<option value="{_e(value)}"{selected}>{_e(label)}</option>')
    return f"""<form action="ui/settings" method="post">
{_ai_prompt_job(settings, "front_photo", "Front Door Still Photo")}
{_ai_prompt_job(settings, "back_photo", "Back Door Still Photo")}
{_ai_prompt_job(settings, "manual_video", "Manual Live Video")}
{_ai_prompt_job(settings, "smart_video", "Smart Live Video Follow-Up")}
{_ai_prompt_job(settings, "detailed_video", "Detailed Live Video Follow-Up")}
<label for="doorbell_video_mode">Live Video Mode</label>
<select id="doorbell_video_mode" name="doorbell_video_mode">{''.join(mode_options)}</select>
<label for="doorbell_live_video_seconds">Live Video Length, Seconds</label>
<input id="doorbell_live_video_seconds" name="doorbell_live_video_seconds" type="number" min="2" max="10" value="{_e(settings.get('doorbell_live_video_seconds', 4))}">
<label for="doorbell_live_video_frames">Live Video Frames To Analyze</label>
<input id="doorbell_live_video_frames" name="doorbell_live_video_frames" type="number" min="2" max="6" value="{_e(settings.get('doorbell_live_video_frames', 4))}">
<button type="submit">Save Doorbell AI Settings</button>
</form>"""


def _ai_prompt_job(settings, job, label):
    styles = settings.get("ai_description_styles") or {}
    custom = settings.get("ai_custom_descriptions") or {}
    selected = styles.get(job) or {
        "front_photo": "balanced",
        "back_photo": "balanced",
        "manual_video": "detailed_blind",
        "smart_video": "fast_security",
        "detailed_video": "detailed_blind",
    }.get(job, "balanced")
    options = []
    for value, text in [
        ("balanced", "Balanced"),
        ("fast_security", "Fast security summary"),
        ("people_movement", "People and movement"),
        ("packages_deliveries", "Packages and deliveries"),
        ("detailed_blind", "Detailed for blind user"),
        ("custom", "Custom"),
    ]:
        selected_attr = " selected" if value == selected else ""
        options.append(f'<option value="{_e(value)}"{selected_attr}>{_e(text)}</option>')
    custom_value = custom.get(job, "")
    return (
        f'<h3>{_e(label)}</h3>'
        f'<label for="ai_style_{_e(job)}">{_e(label)} Style</label>'
        f'<select id="ai_style_{_e(job)}" name="ai_style_{_e(job)}">{"".join(options)}</select>'
        f'<label for="ai_custom_{_e(job)}">{_e(label)} Custom Instructions</label>'
        f'<textarea id="ai_custom_{_e(job)}" name="ai_custom_{_e(job)}" rows="4">{_e(custom_value)}</textarea>'
    )


def _doorbell_test_buttons():
    return (
        '<div class="actions">'
        + _post_button("ui/test/doorbell/front", "", "", "Test Front Doorbell")
        + _post_button("ui/test/doorbell/back", "", "", "Test Back Doorbell")
        + _post_button("ui/test/doorbell_video/front", "", "", "Test Front Live Video")
        + _post_button("ui/test/doorbell_video/back", "", "", "Test Back Live Video")
        + "</div>"
    )


def _fridge_test_buttons():
    return (
        '<div class="actions">'
        + _post_button("ui/test/fridge/fridge", "", "", "Test Fridge Open")
        + _post_button("ui/test/fridge/fridge_closed", "", "", "Test Fridge Closed")
        + _post_button("ui/test/fridge/freezer", "", "", "Test Freezer Open")
        + _post_button("ui/test/fridge/freezer_closed", "", "", "Test Freezer Closed")
        + _post_button("ui/control/ice_maker", "state", "true", "Turn Ice Maker On")
        + _post_button("ui/control/ice_maker", "state", "false", "Turn Ice Maker Off")
        + "</div>"
    )


def _utility_test_buttons():
    return (
        '<div class="actions">'
        + _post_button("ui/test/pushover", "", "", "Send Pushover Test")
        + "</div>"
    )


def _chime_upload_form():
    return """<form action="ui/chimes/upload" method="post" enctype="multipart/form-data">
<label for="chime_file">Upload Chime File</label>
<input id="chime_file" name="file" type="file" accept=".mp3,.wav,.ogg,.m4a">
<button type="submit">Upload Chime</button>
</form>
<form action="ui/chimes/upload-folder" method="post" enctype="multipart/form-data">
<label for="chime_folder">Upload Chime Folder</label>
<input id="chime_folder" name="files" type="file" accept=".mp3,.wav,.ogg,.m4a" multiple webkitdirectory directory>
<button type="submit">Upload Folder</button>
</form>"""


def _chime_assignments(selected, available):
    options = ['<option value="">No chime</option>'] + [
        f'<option value="{_e(item)}">{{selected}}</option>'.replace("{selected}", _e(item)) for item in available
    ]
    forms = ['<p id="chime_assignment_status" class="status-line" role="status" aria-live="polite"></p>']
    for event, label in CHIME_EVENTS:
        current = selected.get(event, "")
        event_options = []
        for item in ["", *available]:
            selected_attr = " selected" if item == current else ""
            text = "No chime" if not item else item
            event_options.append(f'<option value="{_e(item)}"{selected_attr}>{_e(text)}</option>')
        forms.append(
            f'<form action="ui/chimes/assign" method="post"><input type="hidden" name="event" value="{_e(event)}">'
            f'<label for="chime_{_e(event)}">{_e(label)}</label><select id="chime_{_e(event)}" name="filename">{"".join(event_options)}</select>'
            '<div class="actions">'
            '<button type="submit">Save Chime</button>'
            '<button type="button" data-test-selected-chime="true" data-status-target="chime_assignment_status">Test Selected Chime</button>'
            '</div></form>'
        )
    return "".join(forms)


def _manage_chime_files(files):
    return (
        "<details><summary>Manage Uploaded Chime Files</summary>"
        + _chime_upload_form()
        + _chime_files(files)
        + "</details>"
    )


def _chime_files(files):
    if not files:
        return "<p>No uploaded chimes yet.</p>"
    rows = ['<p id="chime_test_status" class="status-line" role="status" aria-live="polite"></p><ul>']
    for filename in files:
        rows.append(
            f"<li>{_e(filename)} "
            + _post_button("ui/chimes/delete", "filename", filename, "Delete")
            + f'<button type="button" data-test-file-chime="true" data-status-target="chime_test_status" data-filename="{_e(filename)}">Test</button>'
            + "</li>"
        )
    rows.append("</ul>")
    return "".join(rows)


def _broadcast_form():
    return """<form action="ui/broadcast" method="post">
<label for="broadcast_message">Broadcast Message</label>
<textarea id="broadcast_message" name="message" rows="3"></textarea>
<button type="submit">Speak Broadcast</button>
</form>"""


def _hvac_form():
    units = [
        ("all", "All Heat Pumps"),
        ("climate.office_heat_pump_alexa", "Office"),
        ("climate.living_room_heat_pump_alexa", "Living Room"),
        ("climate.kitchen_heat_pump_alexa", "Kitchen"),
        ("climate.jamie_s_room_heat_pump_alexa", "Jamie's Room"),
        ("climate.master_bedroom_heat_pump_alexa", "Master Bedroom"),
    ]
    unit_options = "".join(f'<option value="{_e(entity)}">{_e(label)}</option>' for entity, label in units)
    return f"""<form action="ui/hvac" method="post">
<label for="hvac_entity">Unit</label><select id="hvac_entity" name="entity_id">{unit_options}</select>
<label for="hvac_mode">Mode</label><select id="hvac_mode" name="mode"><option value="cool">Cool</option><option value="heat">Heat</option><option value="off">Off</option><option value="">Temperature Only</option></select>
<label for="hvac_temperature">Target Temperature</label><input id="hvac_temperature" name="temperature" type="number" min="50" max="90" step="1" value="70">
<button type="submit">Send Heat Pump Command</button>
</form>"""


def _heat_pump_status(units):
    if not units:
        return "<p>Heat pump status has not refreshed yet.</p>"
    rows = ["<table><thead><tr><th>Unit</th><th>Status</th><th>Target</th><th>Fan</th><th>Swing</th></tr></thead><tbody>"]
    for unit in units:
        attrs = unit.get("attributes") or {}
        state = unit.get("state") or "unknown"
        target = attrs.get("temperature") or attrs.get("target_temp_high") or ""
        fan = attrs.get("fan_mode") or ""
        swing = attrs.get("swing_mode") or ""
        status = state if unit.get("ok") else f"{state} - needs attention"
        rows.append(
            "<tr>"
            f"<td>{_e(unit.get('name') or unit.get('entity_id'))}</td>"
            f"<td>{_e(status)}</td>"
            f"<td>{_e(target)}</td>"
            f"<td>{_e(fan)}</td>"
            f"<td>{_e(swing)}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _airflow_status(units):
    if not units:
        return "<p>Airflow status has not refreshed yet.</p>"
    rows = ["<h3>Airflow</h3><table><thead><tr><th>Control</th><th>Status</th><th>Percent</th><th>Preset</th></tr></thead><tbody>"]
    for unit in units:
        attrs = unit.get("attributes") or {}
        status = unit.get("state") or "unknown"
        if not unit.get("ok"):
            status = f"{status} - needs attention"
        rows.append(
            "<tr>"
            f"<td>{_e(unit.get('name') or unit.get('entity_id'))}</td>"
            f"<td>{_e(status)}</td>"
            f"<td>{_e(attrs.get('percentage', ''))}</td>"
            f"<td>{_e(attrs.get('preset_mode', ''))}</td>"
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _vacuum_form():
    return """<div class="actions">
<form action="ui/vacuum" method="post"><input type="hidden" name="action" value="start"><button type="submit">Start Vacuum</button></form>
<form action="ui/vacuum" method="post"><input type="hidden" name="action" value="pause"><button type="submit">Pause Vacuum</button></form>
<form action="ui/vacuum" method="post"><input type="hidden" name="action" value="dock"><button type="submit">Send Vacuum Home</button></form>
<form action="ui/vacuum" method="post"><input type="hidden" name="action" value="stop"><button type="submit">Stop Vacuum</button></form>
</div>"""


def _vacuum_status(vacuum, status_sensor=None):
    if not vacuum:
        return "<p>Vacuum status has not refreshed yet.</p>"
    status = vacuum.get("state") or "unknown"
    if not vacuum.get("ok"):
        status = f"{status} - needs attention"
    sensor_state = (status_sensor or {}).get("state") or ""
    extra = f"<p><strong>Roborock status:</strong> {_e(sensor_state)}</p>" if sensor_state else ""
    return f"<p><strong>Current vacuum status:</strong> {_e(status)}</p>{extra}"


def _refrigerator_status(refrigerator):
    if not refrigerator:
        return "<p>Refrigerator status has not refreshed yet.</p>"
    labels = {
        "ice_maker": "Ice Maker",
        "fridge_door": "Fridge Door",
        "freezer_door": "Freezer Door",
        "filter_usage": "Water Filter Usage",
        "filter_status": "Filter Status",
    }
    rows = ["<table><thead><tr><th>Item</th><th>Status</th></tr></thead><tbody>"]
    for key, label in labels.items():
        item = refrigerator.get(key) or {}
        status = item.get("state") or "unknown"
        if item and not item.get("ok"):
            status = f"{status} - needs attention"
        rows.append(f"<tr><td>{_e(label)}</td><td>{_e(status)}</td></tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def _settings_form(settings):
    gemini_status = "configured" if settings.get("gemini_configured") else "not configured"
    pushover_status = "configured" if settings.get("pushover_configured") else "not configured"
    return f"""<form action="ui/settings" method="post">
<p><strong>Gemini key:</strong> {_e(gemini_status)}</p>
<p><strong>Pushover:</strong> {_e(pushover_status)}</p>
<label for="external_base_url">External Core URL For Chime Playback</label>
<input id="external_base_url" name="external_base_url" value="{_e(settings.get('external_base_url', ''))}" placeholder="http://homeassistant.local:8099">
<label for="gemini_vision_model">Gemini Vision Model</label>
<input id="gemini_vision_model" name="gemini_vision_model" value="{_e(settings.get('gemini_vision_model', 'gemini-3.5-flash'))}">
<label for="front_door_camera_entity">Front Door Camera Entity</label>
<input id="front_door_camera_entity" name="front_door_camera_entity" value="{_e(settings.get('front_door_camera_entity', 'camera.front_door_snapshot'))}">
<label for="back_door_camera_entity">Back Door Camera Entity</label>
<input id="back_door_camera_entity" name="back_door_camera_entity" value="{_e(settings.get('back_door_camera_entity', 'camera.back_door_snapshot'))}">
<label for="doorbell_dedupe_seconds">Doorbell Duplicate Window, Seconds</label>
<input id="doorbell_dedupe_seconds" name="doorbell_dedupe_seconds" type="number" min="5" max="180" value="{_e(settings.get('doorbell_dedupe_seconds', 30))}">
<label for="fridge_stale_minutes">Refrigerator Door Stale Warning, Minutes</label>
<input id="fridge_stale_minutes" name="fridge_stale_minutes" type="number" min="5" max="240" value="{_e(settings.get('fridge_stale_minutes', 45))}">
<label for="vacuum_repeat_quiet_minutes">Vacuum Repeat Quiet Time, Minutes</label>
<input id="vacuum_repeat_quiet_minutes" name="vacuum_repeat_quiet_minutes" type="number" min="1" max="240" value="{_e(settings.get('vacuum_repeat_quiet_minutes', 20))}">
<label for="vacuum_announce_events">Vacuum Events To Announce</label>
<input id="vacuum_announce_events" name="vacuum_announce_events" value="{_e(', '.join(settings.get('vacuum_announce_events', [])))}">
<label for="gemini_api_key">New Gemini API Key</label>
<input id="gemini_api_key" name="gemini_api_key" type="password" autocomplete="off">
<label><input name="clear_gemini_api_key" type="checkbox" value="true"> Clear saved Gemini key</label>
<label for="pushover_user_key">Pushover User Key</label>
<input id="pushover_user_key" name="pushover_user_key" type="password" autocomplete="off">
<label for="pushover_api_token">Pushover API Token</label>
<input id="pushover_api_token" name="pushover_api_token" type="password" autocomplete="off">
<label><input name="clear_pushover" type="checkbox" value="true"> Clear saved Pushover keys</label>
<button type="submit">Save Settings</button>
</form>"""


def _diagnostics(state):
    devices = state.get("devices") or {}
    runtime = state.get("runtime") or {}
    checks = [
        ("Home Assistant API", (state.get("home_assistant") or {}).get("ok"), (state.get("home_assistant") or {}).get("message")),
        ("Required entities", (state.get("dependencies") or {}).get("ok"), (state.get("dependencies") or {}).get("message")),
        ("FFmpeg", bool(runtime.get("ffmpeg")), runtime.get("ffmpeg") or "not found"),
        ("Heat pumps", bool(devices.get("heat_pumps")) and all(item.get("ok") for item in devices.get("heat_pumps", [])), f"{len(devices.get('heat_pumps') or [])} configured"),
        ("Airflow controls", bool(devices.get("airflow")) and all(item.get("ok") for item in devices.get("airflow", [])), f"{len(devices.get('airflow') or [])} configured"),
        ("Vacuum", (devices.get("vacuum") or {}).get("ok"), (devices.get("vacuum") or {}).get("state", "unknown")),
        ("Refrigerator", all((devices.get("refrigerator") or {}).get(key, {}).get("ok") for key in ("ice_maker", "fridge_door", "freezer_door")), "door sensors and ice maker"),
    ]
    rows = ["<table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead><tbody>"]
    for name, ok, detail in checks:
        rows.append(f"<tr><td>{_e(name)}</td><td>{'OK' if ok else 'Needs attention'}</td><td>{_e(detail or '')}</td></tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def _recent_events(events):
    if not events:
        return "<p>No events yet.</p>"
    rows = ["<ul>"]
    for item in list(events)[-20:][::-1]:
        rows.append(f"<li>{_e(item.get('event_type', 'event'))}: {_e(item.get('message', ''))}</li>")
    rows.append("</ul>")
    return "".join(rows)


def _post_button(action, name, value, label, async_action=False, status_target="async_status"):
    hidden = f'<input type="hidden" name="{_e(name)}" value="{_e(value)}">' if name else ""
    async_attrs = f' data-async="true" data-status-target="{_e(status_target)}"' if async_action else ""
    return f'<form action="{_e(action)}" method="post"{async_attrs}>{hidden}<button type="submit">{_e(label)}</button></form>'


def _display_name(value):
    return str(value or "").title().replace("'S", "'s")


def _e(value):
    return escape(str(value or ""), quote=True)
