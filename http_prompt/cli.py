from __future__ import unicode_literals

import json
from http.cookies import SimpleCookie
from urllib.request import pathname2url, urlopen

import yaml
import os
import re
import sys
import gzip

import click

from httpie.plugins import FormatterPlugin  # noqa, avoid cyclic import
from httpie.output.formatters.colors import Solarized256Style
from prompt_toolkit import prompt, AbortAction
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from prompt_toolkit.layout.lexers import PygmentsLexer
from prompt_toolkit.styles.from_pygments import style_from_pygments
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound

from . import __version__
from . import config
from .completer import HttpPromptCompleter
from .context import Context
from .contextio import load_context, save_context
from .execution import execute
from .lexer import HttpPromptLexer
from .utils import smart_quote
from .xdg import get_data_dir


# XXX: http://click.pocoo.org/python3/#unicode-literals
click.disable_unicode_literals_warning = True


def fix_incomplete_url(url):
    if url.startswith(('s://', '://')):
        url = 'http' + url
    elif url.startswith('//'):
        url = 'http:' + url
    elif not url.startswith(('http://', 'https://')):
        url = 'http://' + url
    return url


def update_cookies(base_value, cookies):
    cookie = SimpleCookie(base_value)
    for k, v in cookies.items():
        cookie[k] = v
    return str(cookie.output(header='', sep=';').lstrip())


class ExecutionListener(object):

    def __init__(self, cfg):
        self.cfg = cfg

    def context_changed(self, context):
        # Dump the current context to HTTP Prompt format
        save_context(context)

    def response_returned(self, context, response):
        if not response.cookies:
            return

        cookie_pref = self.cfg.get('set_cookies') or 'auto'
        if cookie_pref == 'auto' or (
                cookie_pref == 'ask' and
                click.confirm("Cookies incoming! Do you want to set them?")):
            existing_cookie = context.headers.get('Cookie')
            new_cookie = update_cookies(existing_cookie, response.cookies)
            context.headers['Cookie'] = new_cookie
            click.secho('Cookies set: %s' % new_cookie)


def normalize_url(ctx, param, value):
    if value:
        if not re.search(r'^\w+://', value):
            value = 'file:' + pathname2url(os.path.abspath(value))
        return value
    return None


def _urlopen(response):
    # https://gist.github.com/Manouchehri/0ce55d239fb07c41c92f
    if response.info().get('Content-Encoding') == 'gzip':
        pagedata = gzip.decompress(response.read())
    elif response.info().get('Content-Encoding') == 'deflate':
        pagedata = response.read()
    elif response.info().get('Content-Encoding'):
        print('Encoding type unknown')
    else:
        pagedata = response.read()

    return pagedata

@click.command(context_settings=dict(
    ignore_unknown_options=True,
))
@click.option('--spec', help="OpenAPI/Swagger specification file.",
              callback=normalize_url)
@click.option('--env', help="Environment file to preload.",
              type=click.Path(exists=True))
@click.argument('url', default='')
@click.argument('http_options', nargs=-1, type=click.UNPROCESSED)
@click.version_option(message='%(version)s')
def cli(spec, env, url, http_options):
    click.echo('Version: %s' % __version__)

    copied, config_path = config.initialize()
    if copied:
        click.echo('Config file not found. Initialized a new one: %s' %
                   config_path)

    cfg = config.load()

    # Override pager/less options
    os.environ['PAGER'] = cfg['pager']
    os.environ['LESS'] = '-RXF'

    if spec:
        response = urlopen(spec)
        try:
            content = _urlopen(response) # f.read().decode('utf-8')
            try:
                spec = json.loads(content)
            except json.JSONDecodeError:
                try:
                    spec = yaml.load(content)
                except yaml.YAMLError:
                    click.secho("Warning: Specification file '%s' is neither valid JSON nor YAML" %
                                spec, err=True, fg='red')
                    spec = None
        finally:
            response.close()

    if url:
        url = fix_incomplete_url(url)
    context = Context(url, spec=spec)

    output_style = cfg.get('output_style')
    if output_style:
        context.options['--style'] = output_style

    # For prompt-toolkit
    history = FileHistory(os.path.join(get_data_dir(), 'history'))
    lexer = PygmentsLexer(HttpPromptLexer)
    completer = HttpPromptCompleter(context)
    try:
        style_class = get_style_by_name(cfg['command_style'])
    except ClassNotFound:
        style_class = Solarized256Style
    style = style_from_pygments(style_class)

    listener = ExecutionListener(cfg)

    if len(sys.argv) == 1:
        # load previous context if nothing defined
        load_context(context)
    else:
        if env:
            load_context(context, env)
            if url:
                # Overwrite the env url if not default
                context.url = url

        if http_options:
            # Execute HTTPie options from CLI (can overwrite env file values)
            http_options = [smart_quote(a) for a in http_options]
            execute(' '.join(http_options), context, listener=listener)

    while True:
        try:
            text = prompt('%s> ' % context.url, completer=completer,
                          lexer=lexer, style=style, history=history,
                          auto_suggest=AutoSuggestFromHistory(),
                          on_abort=AbortAction.RETRY, vi_mode=cfg['vi'])
        except EOFError:
            break  # Control-D pressed
        else:
            execute(text, context, listener=listener, style=style_class)
            if context.should_exit:
                break

    click.echo("Goodbye!")
