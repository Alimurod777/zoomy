"""Classify Zoom admission/connection states without relying on its URL."""
import re

TERMINAL_STATES = {"ended", "removed", "invalid", "passcode", "app_required", "app_launch", "auth_required"}

def classify_meeting(snapshot):
    if isinstance(snapshot, str):
        snapshot = {"text": snapshot}
    text = re.sub(r"\s+", " ", snapshot.get("text", "")).lower().replace("’", "'")
    phrases = {
        "removed": ("removed from this meeting", "you have been removed", "host has denied", "not allowed to rejoin"),
        "ended": ("been ended by host", "been ended by the host", "meeting has ended", "meeting is ended", "host has ended"),
        "app_required": ("join this meeting using the zoom desktop", "only available in the zoom desktop", "not supported in the web client", "meeting requires the zoom app", "joining this meeting from a web browser is not allowed", "web client is disabled for this meeting"),
        "auth_required": ("sign in to join this meeting", "sign in to join the meeting", "only authenticated users can join"),
        "invalid": ("meeting link is invalid", "invalid meeting id", "meeting id is not valid", "meeting is expired", "meeting is for authorized attendees only"),
        "passcode": ("incorrect meeting passcode", "wrong passcode", "invalid passcode"),
        "disconnected": ("you have been disconnected", "unable to connect", "cannot connect to zoom", "failed to connect", "connection to zoom has been lost", "network connection has been lost", "reconnecting"),
        "waiting_room": ("host will let you in", "host will admit you", "waiting for the host to admit", "please wait to be admitted",
                         "we've let them know you're here", "we've let the host know", "host has been notified that you're waiting"),
        "waiting_host": ("waiting for host", "waiting for the host", "wait for host to start", "wait for the host to start", "meeting has not started", "meeting hasn't started", "host is in another meeting"),
    }
    for state, options in phrases.items():
        if state in {"waiting_room", "waiting_host"} and snapshot.get("waiting_room"):
            return "waiting_room"
        if state == "app_required" and any(option in text for option in ("join from your browser", "join from browser")):
            continue
        if any(phrase in text for phrase in options):
            return state
    if snapshot.get("waiting_room"):
        return "waiting_room"
    if snapshot.get('app_launch') and not snapshot.get('admitted'):
        return 'app_launch'
    if snapshot.get("admitted"):
        return "admitted"
    return "connecting"


SNAPSHOT_JS = r'''() => {
    const visible = el => {if(!el)return false;const r=el.getBoundingClientRect();
        const s=getComputedStyle(el);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
    const any = selectors => [...document.querySelectorAll(selectors)].some(visible);
    const waiting_room=any('#waiting-room-container, .waiting-room-container, .join-meeting-waitingroom');
    // A preview/waiting-room video is not proof of meeting admission.
    const admitted=any('button.footer__leave-btn, .meeting-client .footer__inner')
        || (document.documentElement.hasAttribute('data-zoomy-recording')&&!!document.querySelector('button.footer__leave-btn'));
    // Chat messages/captions must not be mistaken for host or network notices.
    const notices=[...document.querySelectorAll('[role="dialog"],[role="alertdialog"],[role="alert"],.zm-modal,.reconnecting-dialog')]
        .filter(visible).map(el=>el.innerText||'').join(' ');
    const launchLabels=/^(launch (meeting|zoom(?: workplace)?)|open zoom(?: meetings| workplace| app)?|join (?:from|in) (?:the )?app|открыть zoom|запустить конференцию)$/i;
    const app_launch=!admitted&&[...document.querySelectorAll('a,button,[role="button"]')].some(el=>
        visible(el)&&(launchLabels.test((el.innerText||el.getAttribute('aria-label')||'').trim())
            || /^(zoommtg|zoomus):\/\//i.test(el.getAttribute('href')||'')));
    return {text:admitted&&!waiting_room?notices:(document.body.innerText||''),admitted,waiting_room,app_launch};
}'''
