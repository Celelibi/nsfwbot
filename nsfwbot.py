#!/usr/bin/env python3

import logging
import asyncio
import re
import socket
import ssl
import itertools as it
import humanize
import irc.bot
import irc.dict
import irc.connection
import asyncworkflow



################################ Configuration #################################
# List of addresses (for the same network) to try to connect to.
hosts = ["irc.server.net"]
# List of ports to try to connect to. SSL ports should be first.
ports = [6697, 6667]
# List of nicks to try to use.
nicks = ["nsfwbot"]
# List of channels to join.
channels = ["#channel"]
# Real name
realname = "If you post NSFW images, I will tell!"
# NickServ password
nspass = None
# Logging level
loglevel = logging.INFO
# Max size of images to download
max_download_size = 20 * 1024 * 1024

# NickServ nickname
ns_nick = "NickServ"
# NickServ message
nsmsgre = {}
nsmsgre["nick_is_registered"] = r"Ce pseudo est enregistr� et prot�g�.*"
nsmsgre["accepted_password"] = r"Mot de passe accept�.*"
nsmsgre["ghosted"] = r"L'utilisateur .* a �t� d�connect�."
############################# End of configuration #############################



class ConnectionFactory(object):
    """This connection factory is automatically compatible with IPv6."""

    def __init__(self, bind=None, tryssl=True):
        self._bind = bind
        self._tryssl = tryssl

    def connect(self, addr):
        if self._tryssl:
            sock = socket.create_connection(addr, source_address=self._bind)
            try:
                return ssl.wrap_socket(sock)
            except ssl.SSLError:
                pass

            logging.debug("Couldn't connect to %s %d using SSL", *addr)
            sock.close()

        return socket.create_connection(addr, source_address=self._bind)

    __call__ = connect



class NSFWBot(irc.bot.SingleServerIRCBot):
    nickre = re.compile(nicks[0] + r'(\d+)')
    urlre = re.compile(r'https?://[^\s<>()]*')

    def __init__(self, *args, **kwargs):
        super(NSFWBot, self).__init__(*args, **kwargs)
        self.fully_connected = False
        self.identified = False
        self._socket = None
        self._workflow = asyncworkflow.AsyncWorkflow(maxdlsize=max_download_size)
        self._loop = asyncio.get_event_loop()
        self._ident_timeout_handler = None

    # Override SingleServerIRCBot._connect
    def _connect(self):
        super(NSFWBot, self)._connect()
        if self.connection.is_connected():
            self._socket = self.connection.socket
            self._loop.add_reader(self._socket, self.connection.process_data)
        else:
            self._loop.call_later(self.reconnection_interval, self.jump_server)

    # Override SingleServerIRCBot._on_disconnect
    def _on_disconnect(self, c, e):
        self._loop.remove_reader(self._socket)
        self._socket = None
        self.channels = irc.dict.IRCDict()
        self._loop.call_later(self.reconnection_interval, self.jump_server)

    def on_fully_connected(self, cnx, event):
        self.fully_connected = True

        # Bypass identification if there is no password
        if nspass is None or len(nspass) == 0:
            self.on_ready(cnx, event)
            return

        handler = self._loop.call_later(10, self.on_identification_timeout, cnx)
        self._ident_timeout_handler = handler

        if cnx.get_nickname() != nicks[0]:
            cnx.privmsg(ns_nick, "RELEASE %s %s" % (nicks[0], nspass))
            cnx.privmsg(ns_nick, "GHOST %s %s" % (nicks[0], nspass))


    on_nomotd = on_fully_connected
    on_endofmotd = on_fully_connected

    def on_identified(self, cnx, event):
        if self._ident_timeout_handler:
            self._ident_timeout_handler.cancel()
            self._ident_timeout_handler = None

        self.identified = True
        self.on_ready(cnx, event)

    def on_ready(self, cnx, event):
        for c in channels:
            cnx.join(c)

    def on_identification_timeout(self, cnx):
        for c in channels:
            cnx.join(c)
            cnx.privmsg(c, "I couldn't identify to %s. Please help. :(" % ns_nick)

    def on_disconnect(self, cnx, event):
        self.fully_connected = False
        self.identified = False

    def choose_initial_nick(self, cnx, nick, msg):
        # nick is the last one we tried. So try the next one in the list
        try:
            idx = nicks.index(nick)
        except ValueError:
            idx = None

        if idx is not None and idx < len(nicks) - 1:
            cnx.nick(nicks[idx + 1])
            return

        # Generate a new nickname based on nicks[0]
        matches = self.nickre.match(nick)
        if not matches:
            newnum = 0
        else:
            newnum = int(matches.group(1)) + 1

        newnick = nicks[0] + str(newnum)
        while newnick in nicks:
            newnum += 1
            newnick = nicks[0] + str(newnum)

        cnx.nick(newnick)

    def on_nicknameinuse(self, cnx, event):
        if not self.fully_connected:
            self.choose_initial_nick(cnx, *event.arguments)

    def on_erroneusnickname(self, cnx, event):
        if not self.fully_connected:
            self.choose_initial_nick(cnx, *event.arguments)

    def on_privnotice(self, cnx, event):
        nickmask = irc.client.NickMask(event.source)
        if not irc.strings.IRCFoldedCase(nickmask.nick) == ns_nick:
            return

        msg = event.arguments[0]
        if re.match(nsmsgre["nick_is_registered"], msg):
            cnx.privmsg(ns_nick, "IDENTIFY %s" % nspass)
        elif re.match(nsmsgre["accepted_password"], msg):
            self.on_identified(cnx, event)
        elif re.match(nsmsgre["ghosted"], msg):
            cnx.nick(nicks[0])

    def on_pubmsg(self, cnx, event):
        chan = event.target
        if chan not in channels:
            return

        msg = event.arguments[0]
        urls = self.urlre.findall(msg)

        for url in urls:
            asyncio.ensure_future(self._nsfw_report(cnx, chan, url))

    async def _nsfw_report(self, cnx, chan, url):
        try:
            totalsize, istrunc, score = await self._workflow.score_url(url)
        except Exception as e:
            logging.exception(e)
            cnx.privmsg(chan, "<%s>: %s" % (url, e))
            return

        msg = "<%s> " % url
        if totalsize:
            msg += "(%s) " % humanize.naturalsize(totalsize, binary=True)

        if score is None:
            msg += "Can't read as an image."
        else:
            score *= 100
            msg += "NSFW score: %.2f%%. " % score

            if score < 10:
                msg += "Certainly safe."
            elif 10 <= score < 50:
                msg += "Probably safe."
            elif 50 <= score < 90:
                msg += "Probably sexy."
            else:
                msg += "Most likely porn."

        if istrunc:
            mds = humanize.naturalsize(max_download_size, binary=True)
            msg += " The %s download limit was reached." % mds

        cnx.privmsg(chan, msg)

    def start(self):
        self._connect()
        self._loop.call_later(self.reconnection_interval, self._connected_checker)
        self._loop.run_forever()

    def die(self):
        super(NSFWBot, self).die()



def main():
    logfmt = "%(asctime)s %(levelname)s: %(message)s"
    logging.basicConfig(format=logfmt, level=loglevel)

    # A bit of magic to make the decoding work better
    irc.client.ServerConnection.buffer_class.errors = 'replace'

    specs = [irc.bot.ServerSpec(h, p) for h, p in it.product(hosts, ports)]
    bot = NSFWBot(specs, nicks[0], realname, reconnection_interval=10, connect_factory=ConnectionFactory())

    try:
        bot.start()
    except KeyboardInterrupt:
        bot.die()



if __name__ == '__main__':
    main()
