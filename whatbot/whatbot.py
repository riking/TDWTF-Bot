#!/usr/bin/python
# -*- coding: utf8 -*-

from collections import namedtuple
from pprint import pprint
from time import time, sleep
import requests
import random
import re
import ConfigParser
import sys

REPLY_TO_PMS = False # If True, reply to private messages instead of mentions

BASE_URL = "http://what.thedailywtf.com"

class WhatBot(object):
    """
    A Discourse bot.
    """

    class WorseThanFailure(Exception):
        pass

    Mention = namedtuple('Mention', ['username', 'topic_id', 'post_number', 'post_id'])

    def __init__(self):
        self._session = requests.Session()
        self._session.headers['X-Requested-With'] = "XMLHttpRequest"
        self._client_id = self._get_client_id()
        self._bus_registrations = {}
        self._bus_callbacks = {}
        self._polling_functions = []

        self._nbsp_count = random.randrange(0, 50)
        self._autolike_poll_history = {}

        config = ConfigParser.ConfigParser()
        config.read(['whatbot.conf'])
        self._config = config

    def run(self):
        # Get the CSRF token
        res = self._get("/session/csrf", _=int(time() * 1000))
        self._session.headers['X-CSRF-Token'] = res[u'csrf']

        # Login
        res = self._post("/session", login=self._config.get('WhatBot', 'Username'), password=self._config.get('WhatBot', 'Password'))
        if u'error' in res:
            raise self.WorseThanFailure(res[u'error'].encode('utf8'))

        my_uid = res[u'user'][u'id']

        # Set up bus registrations according to feature flags

        if (
            self._config.getboolean('Features', 'SignatureGuy') or
            self._config.getboolean('Features', 'TransferPost')
        ):
            self._bus_register("/notification/%d" % my_uid, self._notif_mentioned)
            self._handle_notifications()

        if self._config.getboolean('Features', 'AutoLike'):
            topics = self._config.get('Params', 'LikingTopics')
            for topic in topics.split():
                topic_id = int(topic)
                self._bus_register("/topic/%d" % topic_id, self._notif_likes_topic)
                self._init_liking(topic_id)
            self._polling_register(self._poll_user_posts)

        self._session.headers['X-SILENCE-LOGGER'] = "true"

        print "Entering main loop"
        try:
            last_poll = -1
            while True:
                pprint(self._bus_registrations)
                data = self._post("/message-bus/%s/poll" % self._client_id,
                    **self._bus_registrations)

                if self._config.getboolean('WhatBot', 'MessageBusDebug'):
                    pprint(data)

                for message in data:
                    channel = message[u'channel']
                    if channel in self._bus_registrations:
                        message_id = message[u'message_id']
                        self._bus_registrations[channel] = message_id
                        self._bus_callbacks[channel](message[u'data'])
                    if channel == u"/__status":
                        for key, value in message[u'data'].iteritems():
                            if key in self._bus_registrations:
                                self._bus_registrations[key] = value

                if last_poll + self._config.getint('Params', 'PollingIntervalSecs') < time():
                    print "Performing polling"
                    for callback in self._polling_functions:
                        callback()
                    last_poll = time()

        except requests.exceptions.HTTPError as e:
            print 'HTTP Error', e
            print e.args
            pprint(e.response)
            pass
        except KeyboardInterrupt:
            print '\nQuitting.'


    def _bus_register(self, channel, callback):
        self._bus_registrations[channel] = -1
        self._bus_callbacks[channel] = callback

    def _polling_register(self, callback):
        self._polling_functions.append(callback)


    def _notif_ignore(self, message):
        pass

    def _notif_mentioned(self, message):
        if REPLY_TO_PMS:
            count = message[u'unread_private_messages']
        else:
            count = message[u'unread_notifications']

        if count > 0:
            self._handle_notifications()

    def _notif_likes_topic(self, message):
        type = message[u'type']
        print("Update: %s post id %d" % (message[u'type'], message[u'id']))
        if type == u'created':
            self._like_post(message[u'id'])

    def _poll_user_posts(self):
        users = self._config.get('Params', 'LikingUsers')
        for user in users.split():
            result = self._get("/user_actions.json",
                      offset=0,
                      username=user,
                      filter=5
            )

            # check if nothing changed
            if (user in self._autolike_poll_history
                    and result[u'user_actions'][0][u'post_id'] == self._autolike_poll_history[user]):
                print("Polling %s for new posts... no change." % user)
                continue

            # lol output hax
            print("Polling %s for new posts..." % user)

            # collect post IDs
            post_ids = []
            for action in result[u'user_actions']:
                post_ids.append(int(action[u'post_id']))

            break_count = 0
            for post_id in post_ids[0:10]:
                result = self._get("/posts/%d.json" % post_id)
                like_action = self._find_like_action(result[u'actions_summary'])
                if not u'acted' in like_action:
                    self._like_post(post_id)
                else:
                    break_count += 1
                    if break_count >= 3:
                        print("Stopped %s new posts check at post %d" % (user, post_id))
                        self._autolike_poll_history[user] = post_ids[0]
                        break

    def _handle_notifications(self):
        for mention in self._get_mentions():
            pprint(mention)
            if self._config.getboolean('Features', 'SignatureGuy'):
                self._handle_mention_sigguy(mention)
            if self._config.getboolean('Features', 'TransferPost'):
                self._handle_mention_transfer(mention)

    def _handle_mention_sigguy(self, mention):
        print u"Replying to %s in topic %d, post %d" % (mention.username,
            mention.topic_id, mention.post_number)

        print u"Marking as read…"
        self._mark_as_read(mention.topic_id, mention.post_number)

        print u"Sending reply…"
        message = self._config.get('Params', 'Message') % mention.username + (u"&nbsp;" *
            self._nbsp_count)
        self._nbsp_count = (self._nbsp_count + 1) % 50

        self._reply_to(mention.topic_id, mention.post_number, message)

    def _handle_mention_transfer(self, mention):
        print u"Reposessing from %s in in topic %d, post %d" % (mention.username,
            mention.topic_id, mention.post_number)

        params = {'username': self._config.get('Params', 'TransferPostTarget'),
                  'post_ids[]': mention.post_id}

        result = self._post("/t/%d/change-owner" % mention.topic_id, **params)

        self._mark_as_read(mention.topic_id, mention.post_number)
        print u"Complete."
        sleep(1)

    def _init_liking(self, topic):
        topic_data = self._get("/t/%d/last.json" % topic)
        for post in topic_data[u'post_stream'][u'posts']:
            actions = post[u'actions_summary']
            like_action = self._find_like_action(actions)
            if not u'acted' in like_action:
                self._like_post(post[u'id'])
            else:
                pprint("Skipping liked post %d" % post[u'id'])

    @staticmethod
    def _find_like_action(actions_summary):
        for action in actions_summary:
            if action[u'id'] == 2:
                return action
        return None

    def _like_post(self, post_id):
        print("Liking post %d" % post_id)
        ret = self._post("/post_actions", id=post_id,
            post_action_type_id=2,
            flag_topic=u'false'
        )
        sleep(.2)
        return ret

    def _reply_to(self, topic_id, post_number, raw_message):
        # No idea what happens if we mix these up
        archetype = 'private_message' if REPLY_TO_PMS else 'regular'

        sleep(5)

        return self._post("/posts", raw=raw_message, topic_id=topic_id,
            reply_to_post_number=post_number,
            archetype=archetype
        )

    def _mark_as_read(self, topic_id, post_number):
        # Send fake timings
        # I hate special chars in POST keys
        kwargs = {
            'topic_id': topic_id,
            'topic_time': 400, # msecs passed on topic (I think)
            'timings[%d]' % post_number: 400 # msecs passed on post (same)
        }

        self._post("/topics/timings", **kwargs)


    def _get_mentions(self):
        watched_type = 6 if REPLY_TO_PMS else 1

        for notification in self._get("/notifications", _=int(time() * 1000)):
            if (notification[u'notification_type'] == watched_type and
                notification[u'read'] == False):
                data = notification[u'data']
                yield self.Mention(
                    username=data[u'original_username'],
                    topic_id=notification[u'topic_id'],
                    post_number=notification[u'post_number'],
                    post_id=data[u'original_post_id'])

    @staticmethod
    def _get_client_id():
        def _replace(letter):
            val = random.randrange(0, 16)
            if letter == "x":
                val = (3 & val) | 8
            return "%x" % val

        return re.sub('[xy]', _replace, "xxxxxxxxxxxx5xxxyxxxxxxxxxxxxxxx")

    def _get(self, url, **kwargs):
        r = self._session.get(BASE_URL + url, params=kwargs)

        if r.status_code == 503:
            self._loop_for_upgrade()
            return self._get(url, **kwargs)

        r.raise_for_status()
        return r.json()

    def _post(self, url, **kwargs):
        r = self._session.post(BASE_URL + url, data=kwargs)

        if r.status_code == 422:
            raise self.WorseThanFailure(u",".join(r.json()[u'errors']))
        if r.status_code == 503:
            self._loop_for_upgrade()
            return self._post(url, **kwargs)

        r.raise_for_status()
        if r.headers['Content-type'].startswith('application/json'):
            return r.json()
        return r.content

    def _loop_for_upgrade(self):
        while True:
            sleep(5)
            r = self._session.get(BASE_URL + "/srv/status")
            if r.status_code < 500:
                return

if __name__ == '__main__':
    WhatBot().run()
