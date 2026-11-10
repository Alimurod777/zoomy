"""Read only the display name from the authenticated Zoom profile UI."""
import re

PROFILE_NAME_JS = '''() => {
    const selectors=['[data-testid="base-info-displayname"]','[data-testid="base-info-display-name"]',
        '[data-testid="display-name"]','#displayname','input[name="displayName"]',
        '[data-testid="base-info-fullname"]'];
    const full=document.querySelector('[data-testid="base-info-fullname"]');
    const subtitle=full?.closest('h2')?.nextElementSibling;
    if(subtitle?.tagName==='P' && subtitle.textContent.trim() && !subtitle.textContent.includes('@'))
        return subtitle.textContent.trim();
    for(const selector of selectors){
        const element=document.querySelector(selector);if(!element)continue;
        const text=(element.value||element.textContent||'').trim();if(text)return text;
    }
    return null;
}'''

def clean_profile_name(name):
    if not isinstance(name, str):
        return None
    name = re.sub(r"\s+", " ", name).strip()
    if not name or len(name) > 80 or "@" in name or name.lower() in {"display name", "edit", "profile"}:
        return None
    return name

async def profile_name_from_page(page):
    try:
        await page.locator('[data-testid="base-info-fullname"], [data-testid="base-info-displayname"], [data-testid="base-info-display-name"], #displayname').first.wait_for(state="visible", timeout=4000)
        return clean_profile_name(await page.evaluate(PROFILE_NAME_JS))
    except Exception:
        return None
