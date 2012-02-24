#!/usr/bin/env python

import redis
import json
import sys
import urllib2
import uuid
from ConfigParser import SafeConfigParser

import game
from templating import load_mustache_env, MustacheRendering

from brubeck.request_handling import Brubeck, WebMessageHandler

try:
    import gevent.timeout as coro_timeout
except ImportError:
    import eventlet.timeout as coro_timeout

###
#
# CONFIG
#
###
if len(sys.argv) != 2:
    print """
Opengold server must be invoked with a single argument, telling it
which mode from `config.ini` to use:

python opengold/server.py <MODE>

Look at `config.ini` for defined modes. Defaults are `production`,
`staging`, and `test`."""
    exit(1)

MODE = sys.argv[1]
PARSER = SafeConfigParser()

DB = 'db'
JS_PATH = 'js_path'
COOKIE_SECRET = 'cookie_secret'
LONGPOLL_TIMEOUT = 'longpoll_timeout'
RECV_SPEC = 'recv_spec'
SEND_SPEC = 'send_spec'

if not len(PARSER.read('config.ini')):
    print "No config.ini file found in this directory.  Writing a config..."

    modes = ['production', 'staging', 'test']
    for i in range(0, len(modes)):
        mode = modes[i]
        PARSER.add_section(mode)
        PARSER.set(mode, DB, str(i))
        PARSER.set(mode, JS_PATH, '/js/build')
        PARSER.set(mode, COOKIE_SECRET, str(uuid.uuid4()))
        PARSER.set(mode, LONGPOLL_TIMEOUT, '20')
        PARSER.set(mode, RECV_SPEC, 'ipc://127.0.0.1:9001')
        PARSER.set(mode, SEND_SPEC, 'ipc://127.0.0.1:9000')

    try:
        conf = open('config.ini', 'w')
        PARSER.write(conf)
        conf.close()
    except IOError:
        print "Could not write config file to `config.ini`, exiting..."
        exit(1)

config = {
    DB: int(PARSER.get(MODE, DB)),
    JS_PATH: PARSER.get(MODE, JS_PATH),
    COOKIE_SECRET: PARSER.get(MODE, COOKIE_SECRET),
    LONGPOLL_TIMEOUT: int(PARSER.get(MODE, LONGPOLL_TIMEOUT)),
    RECV_SPEC: PARSER.get(MODE, RECV_SPEC),
    SEND_SPEC: PARSER.get(MODE, SEND_SPEC),
}

###
#
# DECORATORS
#
###
def unquote_game_name(func):
    """
    Replace the decorated function's first argument with an unquoted
    version of the game_name.
    """
    def wrapped(self, game_name, *args, **kwargs):
        return func(self, urllib2.unquote(game_name), *args, **kwargs)
    return wrapped

def redirect_unless_json(func):
    """
    Returning a 204 cancels the long poll from the refresh, so if this
    was not a json request we have to explicitly direct user back to
    the game.
    """
    def wrapped(self, game_name, *args, **kwargs):
        retval = func(self, game_name, *args, **kwargs)

        if self.status_code == 204:
            if is_json_request(self.message):
                # self.headers['Content-Type'] = 'application/json'
                # self.set_body(json.dumps(self.body))
                return retval
            else:
                # Brubeck's self.redirect() clears cookies, so we can't use it.
                self._finished = True
                self.set_status(302)
                self.headers['Location'] = '/%s/' % game_name
                return self.render()
        else:
            return self.render()

    return wrapped

###
#
# MIXINS
#
###
class PlayerMixin():
    """
    This mixin provides a get_player() method.  The passed game_name
    should already be unquoted, as it will be re-quoted.
    """
    def get_player(self, game_name):
        """
        Get the player for the specified game_name, or None if the
        user is not taking part.  game_name should not be quoted.
        """
        return self.get_cookie(urllib2.quote(game_name, ''), None, self.application.cookie_secret)


###
#
# HELPERS
#
###
def is_json_request(message):
    """
    True if this request was for JSON, False otherwise.
    """
    return message.headers.get('accept').rfind('application/json') > -1


###
#
# HANDLERS
#
###
class GameListHandler(MustacheRendering):

    def get(self):
        """
        List all games currently available.  Only returns if there is
        a game with an ID greater than the provided ID.
        """

        opt_id = []
        try:
            if self.get_argument('id'):
                opt_id.append(int(self.get_argument('id')))
        except ValueError:
            pass

        games = game.games(self.db_conn, *opt_id)

        try:
            context = coro_timeout.with_timeout(config[LONGPOLL_TIMEOUT], games.next)

            if is_json_request(self.message):
                self.headers['Content-Type'] = 'application/json'
                self.set_body(json.dumps(context))
                return self.render()
            else:
                context[JS_PATH] = config[JS_PATH]
                return self.render_template('main', **context)
        except coro_timeout.Timeout:
            if is_json_request(self.message):
                self.set_status(204)
                return self.render()
            else:
                return self.redirect(self.message.path)


class CreateGameHandler(WebMessageHandler):

    def get(self):
        """
        'Create' a game -- really just forward directly to a game page.
        """
        if self.get_argument('name'):
            return self.redirect('/' + self.get_argument('name') + '/')
        else:
            return self.redirect('/')


class ForwardToGameHandler(WebMessageHandler):

    def get(self, game_name):
        """
        Games should be accessed with a trailing slash.
        """
        return self.redirect('/%s/' % game_name)


class GameHandler(MustacheRendering, PlayerMixin):

    @unquote_game_name
    def get(self, game_name):
        """
        Get information about happenings in game since optional id
        argument.  Will hang until something happens after id.  If
        nothing happens for long enough, it will redirect to itself.
        """
        opt_id = []
        try:
            if self.get_argument('id'):
                opt_id.append(int(self.get_argument('id')))
        except ValueError:
            pass

        info = game.info(self.db_conn, game_name, self.get_player(game_name), *opt_id)

        try:
            context = coro_timeout.with_timeout(config[LONGPOLL_TIMEOUT], info.next)
            if is_json_request(self.message):
                self.headers['Content-Type'] = 'application/json'
                self.set_body(json.dumps(context))
                return self.render()
            else:
                context[JS_PATH] = config[JS_PATH]
                return self.render_template('main', **context)
        except coro_timeout.Timeout:
            if is_json_request(self.message):
                self.set_status(204)
                return self.render()
            else:
                return self.redirect(self.message.path)


class ChatHandler(MustacheRendering, PlayerMixin):

    @unquote_game_name
    @redirect_unless_json
    def post(self, game_name):
        """
        Message other players in the game.
        """
        message = self.get_argument('message')
        player = self.get_player(game_name)

        if message and player:
            if game.chat(self.db_conn, game_name, player, message):
                self.set_status(204)
            else:
                self.set_status(400, "Could not send chat")
        elif player is None:
            self.set_status(400, "You are not in this game.")
        else:
            self.set_status(204) # take empty messages gracefully

        return self.render()


class JoinHandler(WebMessageHandler, PlayerMixin):

    @unquote_game_name
    @redirect_unless_json
    def post(self, game_name):
        """
        Try to join the game with the post-specified player name `player`.
        If it succeeds, feed the user a cookie!
        """
        if self.get_player(game_name):  # player already in this game
            self.set_status(400, "You are already in this game")
        elif self.get_argument('player'):
            player_name = self.get_argument('player')
            if game.join(self.db_conn, game_name, player_name):
                self.set_cookie(urllib2.quote(game_name, ''), player_name, self.application.cookie_secret)
                self.set_status(204)
            else:
                self.set_status(400, "Could not add %s to game" % player_name)
        else:
            self.set_status(400, "You must specify a name to join a game.")

        return self.render()


class StartHandler(WebMessageHandler, PlayerMixin):

    @unquote_game_name
    @redirect_unless_json
    def post(self, game_name):
        """
        Vote to enter temple/advance to next round.
        """
        player = self.get_player(game_name)
        if player:
            game.start(self.db_conn, game_name, player)
            self.set_status(204)
        else:
            self.set_status(400, 'You are not in this game')

        return self.render()


class MoveHandler(WebMessageHandler, PlayerMixin):

    @unquote_game_name
    @redirect_unless_json
    def post(self, game_name):
        """
        Looks like we got a Lando!!
        """
        player = self.get_player(game_name)
        if player:
            if game.move(self.db_conn, game_name, player, self.get_argument('move')):
                self.set_status(204)
            else:
                self.set_status(400, 'Could not submit move')
        else:
            self.set_status(400, 'You are not in this game')

        return self.render()

###
#
# BRUBECK RUNNER
#
###
config.update({
    'mongrel2_pair': (config[RECV_SPEC], config[SEND_SPEC]),
    'handler_tuples': [(r'^/$', GameListHandler),
                       (r'^/create$', CreateGameHandler),
                       (r'^/(?P<game_name>[^/]+)$', ForwardToGameHandler),
                       (r'^/(?P<game_name>[^/]+)/$', GameHandler),
                       (r'^/(?P<game_name>[^/]+)/join$', JoinHandler),
                       (r'^/(?P<game_name>[^/]+)/start$', StartHandler),
                       (r'^/(?P<game_name>[^/]+)/move$', MoveHandler),
                       (r'^/(?P<game_name>[^/]+)/chat$', ChatHandler)],
    'cookie_secret': config[COOKIE_SECRET],
    'db_conn': redis.StrictRedis(db=config[DB]),
    'template_loader': load_mustache_env('./templates')
})

opengold = Brubeck(**config)
opengold.run()