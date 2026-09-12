"""Screen grouping is approximate; observations and every arrival remain durable."""

import hashlib
import json

from swarmci.adapters.tracing import attributes, traced


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()[:24]


OBSERVE = r"""() => {
 const visible = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
 const selector = e => {
   if(e.dataset.testid) return '[data-testid="'+CSS.escape(e.dataset.testid)+'"]';
   if(e.id) return '#'+CSS.escape(e.id);
   let parts=[]; while(e && e.nodeType===1){
     let i=1,p=e; while((p=p.previousElementSibling)) if(p.tagName===e.tagName)i++;
     parts.unshift(e.tagName.toLowerCase()+':nth-of-type('+i+')'); e=e.parentElement;
   } return parts.join(' > ');
 };
 const controls = [...document.querySelectorAll('button,a,input,textarea,select,[role="button"],[role="tab"],[role="menuitem"],[contenteditable="true"]')]
 .filter(visible).map(e=>({selector:selector(e),tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),
 label:(e.getAttribute('aria-label')||e.getAttribute('placeholder')||e.innerText||e.getAttribute('name')||'').trim().replace(/\s+/g,' '),
 value:e.type==='password'?'[redacted]':e.value,disabled:!!e.disabled,checked:e.checked,
 selected:e.getAttribute('aria-selected'),expanded:e.getAttribute('aria-expanded'),href:e.getAttribute('href')}));
 return {url:location.href,title:document.title,controls, text:document.body.innerText.slice(0,20000),
 dialogs:[...document.querySelectorAll('[role="dialog"],dialog[open]')].filter(visible).map(e=>e.innerText),
 storage:{local:{...localStorage},session:{...sessionStorage}},scroll:[scrollX,scrollY],historyLength:history.length};
}"""


@traced("Observe and fingerprint state")
async def observe(page, target, path):
    value = await page.evaluate(OBSERVE)
    app = await page.evaluate(target.state_probe) if target.state_probe else None
    fixture = getattr(page, "_swarm_fixture", None)
    if fixture:
        app = await fixture.snapshot()
        value = fixture.normalize(value)
    screen = {
        "url": value["url"],
        "controls": [
            {k: c.get(k) for k in ("tag", "role", "label", "disabled", "selected", "expanded")}
            for c in value["controls"]
        ],
        "dialogs": value["dialogs"],
    }
    screen_key = digest(screen)
    # Hash secrets/storage, never return raw storage in public graph payloads.
    storage_key = digest(value.pop("storage"))
    exact = {**value, "storage_key": storage_key, "app": app}
    if fixture:
        # Locator addresses can change when portals mount or an agent attaches.
        # Retain control order, values and state; locator syntax is replay metadata.
        exact["controls"] = [{k: v for k, v in c.items() if k != "selector"} for c in value["controls"]]
    # In incomplete environments, history is conservatively part of identity.
    if not target.state_probe:
        exact["history"] = [a.model_dump() for a in path]
    attributes(
        state_id=digest(exact), screen_key=screen_key, depth=len(path), controls_count=len(value["controls"])
    )
    return {
        "id": digest(exact),
        "screen_key": screen_key,
        "title": value["title"],
        "url": value["url"],
        "controls": value["controls"],
        "text": value["text"],
        "app_key": digest(app),
        "fingerprint_parts": {k: digest(v) for k, v in exact.items()},
        "depth": len(path),
        "label": (app or {}).get("stage", value["title"]) if isinstance(app, dict) else value["title"],
    }
