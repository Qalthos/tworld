
import tornado.gen
import motor

import twcommon.misc
from twcommon import wcproto
from twcommon.excepts import MessageException, ErrorMessageException

import two.execute

class Command:
    # As commands are defined with the @command decorator, they are stuffed
    # in this dict.
    all_commands = {}

    def __init__(self, name, func, isserver=False, noneedmongo=False, preconnection=False, doeswrite=False):
        self.name = name
        self.func = tornado.gen.coroutine(func)
        self.isserver = isserver
        self.noneedmongo = noneedmongo
        self.preconnection = preconnection
        self.doeswrite = doeswrite
        
    def __repr__(self):
        return '<Command "%s">' % (self.name,)


def command(name, **kwargs):
    """Decorator for command functions.
    """
    def wrap(func):
        cmd = Command(name, func, **kwargs)
        if name in Command.all_commands:
            raise Exception('Command name defined twice: "%s"', name)
        Command.all_commands[name] = cmd
        return cmd
    return wrap

def define_commands():
    """
    Define all the commands which will be used by the server. Return them
    in a dict.

    Note that the last argument will be a IOStream for server commands,
    but a PlayerConnection for player commands. Never the twain shall
    meet.

    These functions wind up as entries in the Command.all_commands dict.
    The arguments to @command wind up as properties of the Command object
    that wraps the function. Oh, and the function is always a
    tornado.gen.coroutine -- you don't need to declare that.
    """

    @command('connect', isserver=True, noneedmongo=True)
    def cmd_connect(app, task, cmd, stream):
        assert stream is not None, 'Tweb connect command from no stream.'
        stream.write(wcproto.message(0, {'cmd':'connectok'}))

        # Accept any connections that tweb is holding.
        for connobj in cmd.connections:
            if not app.mongodb:
                # Reject the players.
                stream.write(wcproto.message(0, {'cmd':'playernotok', 'connid':connobj.connid, 'text':'The database is not available.'}))
                continue
            conn = app.playconns.add(connobj.connid, connobj.uid, connobj.email, stream)
            stream.write(wcproto.message(0, {'cmd':'playerok', 'connid':conn.connid}))
            app.queue_command({'cmd':'connrefreshall', 'connid':conn.connid})
            app.log.info('Player %s has reconnected (uid %s)', conn.email, conn.uid)
            # But don't queue a portin command, because people are no more
            # likely to be in the void than usual.

    @command('disconnect', isserver=True, noneedmongo=True)
    def cmd_disconnect(app, task, cmd, stream):
        for (connid, conn) in app.playconns.as_dict().items():
            if conn.twwcid == cmd.twwcid:
                try:
                    app.playconns.remove(connid)
                except:
                    pass
        app.log.warning('Tweb has disconnected; now %d connections remain', len(app.playconns.as_dict()))

    @command('checkdisconnected', isserver=True, doeswrite=True)
    def cmd_checkdisconnected(app, task, cmd, stream):
        # Construct a list of players who are in the world, but
        # disconnected.
        ls = []
        inworld = 0
        cursor = app.mongodb.playstate.find({'iid':{'$ne':None}},
                                            {'_id':1})
        while (yield cursor.fetch_next):
            playstate = cursor.next_object()
            conncount = app.playconns.count_for_uid(playstate['_id'])
            inworld += 1
            if not conncount:
                ls.append(playstate['_id'])
        cursor.close()

        app.log.info('checkdisconnected: %d players in world, %d are disconnected', inworld, len(ls))
        ### Keep a two-strikes list, so that players are knocked out after some minimum interval
        for uid in ls:
            app.queue_command({'cmd':'tovoid', 'uid':uid, 'portin':False})

    @command('tovoid', isserver=True, doeswrite=True)
    def cmd_tovoid(app, task, cmd, stream):
        # If portto is None, we'll wind up porting to the player's panic
        # location.
        portto = getattr(cmd, 'portto', None)
        
        task.write_event(cmd.uid, 'The world fades away.') ###localize
        others = yield task.find_locale_players(uid=cmd.uid, notself=True)
        if others:
            res = yield motor.Op(app.mongodb.players.find_one,
                                 {'_id':cmd.uid},
                                 {'name':1})
            playername = res['name']
            task.write_event(others, '%s disappears.' % (playername,)) ###localize
        yield motor.Op(app.mongodb.playstate.update,
                       {'_id':cmd.uid},
                       {'$set':{'focus':None, 'iid':None, 'locid':None,
                                'portto':portto,
                                'lastmoved':twcommon.misc.now()}})
        task.set_dirty(cmd.uid, DIRTY_FOCUS | DIRTY_LOCALE | DIRTY_WORLD | DIRTY_POPULACE)
        task.set_data_change( ('playstate', cmd.uid, 'iid') )
        task.set_data_change( ('playstate', cmd.uid, 'locid') )
        if cmd.portin:
            app.schedule_command({'cmd':'portin', 'uid':cmd.uid}, 1.5)
        
    @command('logplayerconntable', isserver=True, noneedmongo=True)
    def cmd_logplayerconntable(app, task, cmd, stream):
        app.playconns.dumplog()
        
    @command('connrefreshall', isserver=True, doeswrite=True)
    def cmd_connrefreshall(app, task, cmd, stream):
        # Refresh one connection (not all the player's connections!)
        conn = app.playconns.get(cmd.connid)
        if not conn:
            return
        task.set_dirty(conn, DIRTY_ALL)
        app.queue_command({'cmd':'connupdateplist', 'connid':cmd.connid})
        ### probably queue a connupdatefriends, too
    
    @command('connupdateplist', isserver=True)
    def cmd_connupdateplist(app, task, cmd, stream):
        # Re-send the player's portlist to one connection.
        conn = app.playconns.get(cmd.connid)
        if not conn:
            return
        player = yield motor.Op(app.mongodb.players.find_one,
                                {'_id':conn.uid},
                                {'plistid':1})
        if not player:
            return
        playstate = yield motor.Op(app.mongodb.playstate.find_one,
                                   {'_id':conn.uid},
                                   {'iid':1})
        if not playstate:
            return
        plistid = player['plistid']
        iid = playstate['iid']
        cursor = app.mongodb.portals.find({'plistid':plistid})
        ls = []
        while (yield cursor.fetch_next):
            portal = cursor.next_object()
            ls.append(portal)
        cursor.close()
        ls.sort(key=lambda portal:portal.get('listpos', 0))
        subls = []
        for portal in ls:
            ### short-scope-name flag?
            desc = yield two.execute.portal_description(app, portal, conn.uid, uidiid=iid, location=True)
            if desc:
                desc['portid'] = str(portal['_id'])
                subls.append(desc)
        app.log.info('### sending plist update: %s', subls)
        conn.write({'cmd':'updateplist', 'plist':subls})
        
    @command('playeropen', noneedmongo=True, preconnection=True)
    def cmd_playeropen(app, task, cmd, conn):
        assert conn is None, 'playeropen command with connection not None'
        connid = cmd._connid
        
        if not app.mongodb:
            # Reject the players anyhow.
            try:
                cmd._stream.write(wcproto.message(0, {'cmd':'playernotok', 'connid':connid, 'text':'The database is not available.'}))
            except:
                pass
            return
            
        conn = app.playconns.add(connid, cmd.uid, cmd.email, cmd._stream)
        cmd._stream.write(wcproto.message(0, {'cmd':'playerok', 'connid':connid}))
        app.queue_command({'cmd':'connrefreshall', 'connid':connid})
        app.log.info('Player %s has connected (uid %s)', conn.email, conn.uid)
        # If the player is in the void, put them somewhere.
        app.queue_command({'cmd':'portin', 'uid':conn.uid})

    @command('playerclose')
    def cmd_playerclose(app, task, cmd, conn):
        app.log.info('Player %s has disconnected (uid %s)', conn.email, conn.uid)
        try:
            app.playconns.remove(conn.connid)
        except Exception as ex:
            app.log.error('Failed to remove on playerclose %d: %s', conn.connid, ex)
    
    @command('portin', isserver=True, doeswrite=True)
    def cmd_portin(app, task, cmd, stream):
        # When a player is in the void, this command should come along
        # shortly thereafter and send them to a destination.
        player = yield motor.Op(app.mongodb.players.find_one,
                                {'_id':cmd.uid},
                                {'name':1, 'scid':1})
        playstate = yield motor.Op(app.mongodb.playstate.find_one,
                                   {'_id':cmd.uid})
        if not player or not playstate:
            raise ErrorMessageException('Portin: no such player: %s' % (cmd.uid,))
        playername = player['name']
        if playstate.get('iid', None) and playstate.get('locid', None):
            app.log.info('Player %s is already in the world', playername)
            return
        # Figure out what destination was set. If none, default to the
        # start world. ### Player's chosen panic location!
        newloc = playstate.get('portto', None)
        if newloc:
            newwid = newloc['wid']
            newscid = newloc['scid']
            newlocid = newloc['locid']
        else:
            res = yield motor.Op(app.mongodb.config.find_one,
                                 {'key':'startworldloc'})
            lockey = res['val']
            res = yield motor.Op(app.mongodb.config.find_one,
                                 {'key':'startworldid'})
            newwid = res['val']
            newscid = player['scid']
            res = yield motor.Op(app.mongodb.locations.find_one,
                                 {'wid':newwid, 'key':lockey})
            newlocid = res['_id']
        app.log.info('### player portin to %s, %s, %s', newwid, newscid, newlocid)
        
        instance = yield motor.Op(app.mongodb.instances.find_one,
                                  {'wid':newwid, 'scid':newscid})
        if instance:
            minaccess = instance.get('minaccess', ACC_VISITOR)
        else:
            minaccess = ACC_VISITOR
        if False: ### check minaccess against scope access!
            task.write_event(cmd.uid, 'You do not have access to this instance.') ###localize
            return
        
        if instance:
            newiid = instance['_id']
        else:
            newiid = yield motor.Op(app.mongodb.instances.insert,
                                    {'wid':newwid, 'scid':newscid})
            app.log.info('Created instance %s (world %s, scope %s)', newiid, newwid, newscid)

        yield motor.Op(app.mongodb.playstate.update,
                       {'_id':cmd.uid},
                       {'$set':{'iid':newiid,
                                'locid':newlocid,
                                'focus':None,
                                'lastmoved': task.starttime,
                                'portto':None }})
        task.set_dirty(cmd.uid, DIRTY_FOCUS | DIRTY_LOCALE | DIRTY_WORLD | DIRTY_POPULACE)
        task.set_data_change( ('playstate', cmd.uid, 'iid') )
        task.set_data_change( ('playstate', cmd.uid, 'locid') )
        
        # We set everybody in the destination room DIRTY_POPULACE.
        others = yield task.find_locale_players(uid=cmd.uid, notself=True)
        if others:
            task.set_dirty(others, DIRTY_POPULACE)
            task.write_event(others, '%s appears.' % (playername,)) ###localize
        task.write_event(cmd.uid, 'You are somewhere new.')
        
    @command('uiprefs')
    def cmd_uiprefs(app, task, cmd, conn):
        # Could we handle this in tweb? I guess, if we cared.
        # Note that this command isn't marked writable, because it only
        # writes to an obscure collection that never affects anybody's
        # display.
        for (key, val) in cmd.map.__dict__.items():
            res = yield motor.Op(app.mongodb.playprefs.update,
                                 {'uid':conn.uid, 'key':key},
                                 {'uid':conn.uid, 'key':key, 'val':val},
                                 upsert=True)

    @command('meta')
    def cmd_meta(app, task, cmd, conn):
        ls = cmd.text.split()
        if not ls:
            raise MessageException('You must supply a command after the slash. Try \u201C/help\u201D.')
        key = ls[0]
        newcmd = Command.all_commands.get('meta_'+key)
        if not newcmd:
            raise MessageException('Command \u201C/%s\u201D not understood. Try \u201C/help\u201D.' % (key,))
        app.queue_command({'cmd':newcmd.name, 'args':ls[1:]}, connid=conn.connid)

    @command('meta_help')
    def cmd_meta_help(app, task, cmd, conn):
        raise MessageException('No slash commands are currently implemented.')

    @command('meta_refresh')
    def cmd_meta_refresh(app, task, cmd, conn):
        conn.write({'cmd':'message', 'text':'Refreshing display...'})
        app.queue_command({'cmd':'connrefreshall', 'connid':conn.connid})
        
    @command('meta_actionmaps')
    def cmd_meta_actionmaps(app, task, cmd, conn):
        ### debug
        val = 'Locale action map: %s' % (conn.localeactions,)
        conn.write({'cmd':'message', 'text':val})
        val = 'Populace action map: %s' % (conn.populaceactions,)
        conn.write({'cmd':'message', 'text':val})
        val = 'Focus action map: %s' % (conn.focusactions,)
        conn.write({'cmd':'message', 'text':val})

    @command('meta_dependencies')
    def cmd_meta_dependencies(app, task, cmd, conn):
        ### debug
        val = 'Locale dependency set: %s' % (conn.localedependencies,)
        conn.write({'cmd':'message', 'text':val})
        val = 'Populace dependency set: %s' % (conn.populacedependencies,)
        conn.write({'cmd':'message', 'text':val})
        val = 'Focus dependency set: %s' % (conn.focusdependencies,)
        conn.write({'cmd':'message', 'text':val})
        
    @command('meta_exception')
    def cmd_meta_exception(app, task, cmd, conn):
        ### debug
        raise Exception('You asked for an exception.')

    @command('meta_panic')
    def cmd_meta_panic(app, task, cmd, conn):
        app.queue_command({'cmd':'tovoid', 'uid':conn.uid, 'portin':True})

    @command('meta_panicstart')
    def cmd_meta_panicstart(app, task, cmd, conn):
        app.queue_command({'cmd':'portstart'}, connid=task.connid, twwcid=task.twwcid)

    @command('meta_holler')
    def cmd_meta_holler(app, task, cmd, conn):
        ### admin only!
        val = 'Admin broadcast: ' + (' '.join(cmd.args))
        for stream in app.webconns.all():
            stream.write(wcproto.message(0, {'cmd':'messageall', 'text':val}))

    @command('portstart', doeswrite=True)
    def cmd_portstart(app, task, cmd, conn):
        # Fling the player back to the start world. (Not necessarily the
        # same as a panic or initial login!)
        player = yield motor.Op(app.mongodb.players.find_one,
                                {'_id':conn.uid},
                                {'scid':1})
        res = yield motor.Op(app.mongodb.config.find_one,
                             {'key':'startworldloc'})
        lockey = res['val']
        res = yield motor.Op(app.mongodb.config.find_one,
                             {'key':'startworldid'})
        newwid = res['val']
        newscid = player['scid']
        res = yield motor.Op(app.mongodb.locations.find_one,
                             {'wid':newwid, 'key':lockey})
        newlocid = res['_id']
        
        app.queue_command({'cmd':'tovoid', 'uid':conn.uid, 'portin':True,
                           'portto':{'wid':newwid, 'scid':newscid, 'locid':newlocid}})
        
        
    @command('selfdesc', doeswrite=True)
    def cmd_selfdesc(app, task, cmd, conn):
        if getattr(cmd, 'pronoun', None):
            if cmd.pronoun not in ("he", "she", "it", "they", "name"):
                raise ErrorMessageException('Invalid pronoun: %s' % (cmd.pronoun,))
            yield motor.Op(app.mongodb.players.update,
                           {'_id':conn.uid},
                           {'$set': {'pronoun':cmd.pronoun}})
            task.set_data_change( ('players', conn.uid, 'pronoun') )
        if getattr(cmd, 'desc', None):
            yield motor.Op(app.mongodb.players.update,
                           {'_id':conn.uid},
                           {'$set': {'desc':cmd.desc}})
            task.set_data_change( ('players', conn.uid, 'desc') )
        
    @command('say')
    def cmd_say(app, task, cmd, conn):
        res = yield motor.Op(app.mongodb.players.find_one,
                             {'_id':conn.uid},
                             {'name':1})
        playername = res['name']
        if cmd.text.endswith('?'):
            (say, says) = ('ask', 'asks')
        elif cmd.text.endswith('!'):
            (say, says) = ('exclaim', 'exclaims')
        else:
            (say, says) = ('say', 'says')
        val = 'You %s, \u201C%s\u201D' % (say, cmd.text,)
        task.write_event(conn.uid, val)
        others = yield task.find_locale_players(notself=True)
        if others:
            oval = '%s %s, \u201C%s\u201D' % (playername, says, cmd.text,)
            task.write_event(others, oval)

    @command('pose')
    def cmd_pose(app, task, cmd, conn):
        res = yield motor.Op(app.mongodb.players.find_one,
                             {'_id':conn.uid},
                             {'name':1})
        playername = res['name']
        val = '%s %s' % (playername, cmd.text,)
        everyone = yield task.find_locale_players()
        task.write_event(everyone, val)

    @command('action', doeswrite=True)
    def cmd_action(app, task, cmd, conn):
        # First check that the action is one currently visible to the player.
        action = conn.localeactions.get(cmd.action)
        if action is None:
            action = conn.focusactions.get(cmd.action)
        if action is None:
            action = conn.populaceactions.get(cmd.action)
        if action is None:
            raise ErrorMessageException('Action is not available.')
        res = yield two.execute.perform_action(app, task, conn, action)
        
    @command('dropfocus', doeswrite=True)
    def cmd_dropfocus(app, task, cmd, conn):
        playstate = yield motor.Op(app.mongodb.playstate.find_one,
                                   {'_id':conn.uid},
                                   {'focus':1})
        app.log.info('### playstate: %s', playstate)
        yield motor.Op(app.mongodb.playstate.update,
                       {'_id':conn.uid},
                       {'$set':{'focus':None}})
        task.set_dirty(conn.uid, DIRTY_FOCUS)
        
    return Command.all_commands

from two.task import DIRTY_ALL, DIRTY_WORLD, DIRTY_LOCALE, DIRTY_POPULACE, DIRTY_FOCUS
from twcommon.access import ACC_VISITOR
