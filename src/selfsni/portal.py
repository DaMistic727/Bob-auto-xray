import secrets
from pathlib import Path
from .core import ROOT, token, write

def generate(destination):
    theme = secrets.choice([
        ('#286757', '#f1f3ef', '#e9f1e9'), ('#485e94', '#f0f2f7', '#e9edf6'),
        ('#86554b', '#f7f3f0', '#f4eae3'), ('#66538a', '#f3f1f7', '#ede8f5')])
    name = secrets.choice(['Alder', 'Cairn', 'Orin', 'Vale', 'Luma', 'Nacre']) + secrets.choice(['space', 'nest', 'vault', 'room', 'haven'])
    values = {k: 's' + token(8) for k in ['COOKIE', 'AUTH', 'CSRF', 'ICON', 'STYLE',
              'LAYOUT', 'ASIDE', 'CARD', 'ART', 'USERID', 'PASSID', 'USERFIELD', 'PASSFIELD']}
    values.update(NAME=name, ACCENT=theme[0], PAPER=theme[1], SIDEBG=theme[2],
                  RADIUS=str(12 + secrets.randbelow(15)),
                  TAGLINE=secrets.choice(['Make space for good work.', 'Everything in its own place.', 'Your ideas, together.']))
    def render(template):
        for key, value in values.items():
            template = template.replace('@@' + key + '@@', value)
        assert '@@' not in template
        return template
    dest = Path(destination)
    write(dest / 'index.php', render((ROOT / 'templates/portal.php').read_text()), 0o644)
    write(dest / (values['STYLE'] + '.css'), render((ROOT / 'templates/portal.css').read_text()), 0o644)
    # Original geometric mark, varied in rotation, corner radius, and offsets.
    angle = secrets.randbelow(30) - 15
    radius = 5 + secrets.randbelow(9)
    mark = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" rx="{radius}" fill="{theme[0]}"/><g transform="rotate({angle} 24 24)" fill="none" stroke="white" stroke-width="2"><path d="M12 18 24 12 36 18 24 24Z M12 24 24 30 36 24 M12 30 24 36 36 30"/></g></svg>'
    write(dest / (values['ICON'] + '.svg'), mark, 0o644)
    dest.chmod(0o755)
    return values
