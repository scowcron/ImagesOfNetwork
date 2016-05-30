﻿import asyncio
from collections import deque
import datetime
import logging
import re

import discord
import github3
from praw.errors import HTTPException
import requests

from images_of import settings


RUN_INTERVAL = 2 # minutes
STATS_INTERVAL = 15 # minutes
LOG = logging.getLogger(__name__)

# message type mapping
TYPES = {
    't1':'comment',
    't2':'account',
    't3':'link',
    't4':'message',
    't5':'subreddit',
    't6':'award',
    't8':'promocampaign'
}
MODLOG_ACTIONS = ['invitemoderator', 'acceptmoderatorinvite', 'removemoderator']
EVENT_FILTER = ['IssuesEvent', 'PullRequestEvent', 'PushEvent']
#edited, unlabeled, unassigned, assigned, labeled
ISSUE_ACTION_FILTER = ["opened", "closed", "reopened"]
PULL_REQUEST_ACTION_FILTER = ["opened", "edited", "closed", "reopened", "synchronize"]

#Regex pattern for identifying and stripping out markdown links
MD_LINK_PATTERN = r'(\[)([^\]()#\n]+)\]\(([^\]()#\n]+)\)'
MD_LINK_RE = re.compile(MD_LINK_PATTERN, flags=re.IGNORECASE)


class DiscordBot:
    """Discord Announcer Bot to relay important and relevant network information to
    designated Discord channels on regular intervals."""
    def __init__(self, reddit):
        self.reddit = reddit
        self.run_init = True

        self._setup_client()

        self.last_oc_id = dict()
        self.oc_stream_placeholder = dict()
        self.last_modlog_action = dict()

        self.count_messages = 0
        self.count_oc = 0
        self.count_gh_events = 0
        self.count_modlog = 0

        self.ghub = github3.login(token=settings.GITHUB_OAUTH_TOKEN)
        repo = self.ghub.repository(settings.GITHUB_REPO_USER, settings.GITHUB_REPO_NAME)
        self.last_github_event = repo.iter_events(number=1).next().id

    def _setup_client(self):
        self.client = discord.Client()
        self.client.event(self._on_ready)

    ## ======================================================

    async def _relay_inbox_message(self, message):

        self.count_messages += 1

        # Determine if it's a message that we do NOT want to relay...
        if self._is_relayable_message(message):

            if 'false positive' in message.body.lower():
                LOG.info('[Inbox] Announcing false-positive reply.')
                notification = "New __false-positive__ report from **/u/{}**:\r\n{}\r\n ".format(
                    message.author.name, message.permalink[:-7])

                await self.client.send_message(self.falsepos_chan, notification)
                message.mark_as_read()
            else:
                LOG.info('[Inbox] Announcing inbox message.')
                notification = self._format_message(message)
                await self.client.send_message(self.inbox_chan, notification)
                message.mark_as_read()


    ##------------------------------------

    @staticmethod
    def _is_relayable_message(message):
        """
        Determines if an inbox message is a type of message that should or should not be relayed
        to Discord. Does not relay mod removal or remove replies, blacklist requests, or messages
        from AutoModerator/reddit itself.
        """
        #only matching remove exactly
        if (message.body == 'remove') or ('mod removal' in message.body):
            # Don't announce 'remove' or 'mod removal' replies
            message.mark_as_read()
            LOG.info('[Inbox] Not announcing message type: "remove"/"mod removal"')
            return False

        elif message.subject.lower() == 'please blacklist me':
            # Don't announce blacklist requests
            message.mark_as_read()
            LOG.info('[Inbox] Not announcing message type: "blacklist request"')
            return False

        elif (message.author.name == 'AutoModerator') or (message.author.name == 'reddit'):
            # Don't announce AutoModerator or reddit messages
            message.mark_as_read()
            LOG.info('[Inbox] Not announcing message type: "AutoMod Response"')
            return False

        else:
            return True

    ##------------------------------------

    @staticmethod
    def _format_message(message):
        msg_body = message.body
        msg_body = re.sub('\n\n', '\n', msg_body)

        #Strip markdown hyperlinks, append to bottom
        msg_links = MD_LINK_RE.findall(msg_body)

        if msg_links:
            msg_body = MD_LINK_RE.sub(r'\g<2>', msg_body)
            msg_body += '\r\n __*Links:*__\n'
            i = 0
            for msg in msg_links:
                i += 1
                msg_body += '{}: {}\n'.format(i, msg[2])

        msg_type = TYPES[message.name[:2]]
        if msg_type == 'comment':
            if message.is_root:
                msg_type = 'post comment'
            else:
                msg_type = 'comment reply'

        notification = ("New __{}__ from **/u/{}**: \n```\n{}\n```".format(
            msg_type, message.author.name, msg_body))

        #Add permalink for comment replies
        if message.name[:2] == 't1':
            notification += ('\n**Permalink:** {}?context=10\r\n '.format(message.permalink))

        return notification


    ## ======================================================

    async def _process_oc_stream(self, multi):
        LOG.debug('[OC] Checking for new OC submissions...')
        oc_multi = self.reddit.get_multireddit(settings.MULTI_OWNER, multi)

        if self.oc_stream_placeholder.get(multi, None) is None:
            limit = 125
        else:
            limit = round(25 * RUN_INTERVAL)

        oc_stream = list(oc_multi.get_new(limit=limit, place_holder=self.oc_stream_placeholder.get(multi, None)))
        LOG.debug('[OC] len(oc_stream)=%s oc_stream_placeholder=%s',
                  len(oc_stream), self.oc_stream_placeholder.get(multi, None))

        x = 0
        for submission in oc_stream:
            x += 1
            if submission.id == self.last_oc_id.get(multi, None):
                LOG.debug('[OC] Found last announced %s OC; stopping processing', multi)
                break

            elif submission.id == self.oc_stream_placeholder.get(multi, None):
                LOG.debug('[OC] Found start of last %s stream; stopping processing', multi)
                break

            else:
                if submission.author.name.lower() != settings.USERNAME:

                    self.last_oc_id[multi] = submission.id

                    LOG.info('[OC] OC Post from /u/%s found: %s',
                             submission.author.name, submission.permalink)

                    await self.client.send_message(self.oc_chan,
                                              '---\n**New __OC__** by **/u/{}**:\r\n{}'.format(
                                                  submission.author.name, submission.permalink))


        self.oc_stream_placeholder[multi] = oc_stream[0].id

        self.count_oc += x
        LOG.info('[OC] Proccessed %s %s items', x, multi)
    ## ======================================================

    async def _process_github_events(self):

        repo = self.ghub.repository(settings.GITHUB_REPO_USER, settings.GITHUB_REPO_NAME)

        max_length = (10 * RUN_INTERVAL)

        event_queue = deque(maxlen=max_length)

        e_i = repo.iter_events(number=max_length)

        cont_loop = True
        date_max = (datetime.datetime.today() + datetime.timedelta(days=-1)).utctimetuple()

        LOG.debug('[GitHub] Loading events from GitHub...')
        while cont_loop:
            event = e_i.next()

            if event.id == self.last_github_event:
                cont_loop = False
                continue

            self.count_gh_events += 1

            if len(event_queue) == max_length:
                cont_loop = False
                continue

            if event.created_at.utctimetuple() >= date_max:
                if event.type in EVENT_FILTER:
                    event_queue.append(event)

            else:
                cont_loop = False


        LOG.info('[GitHub] New GitHub Events: %s', len(event_queue))

        #All events queued... now send events to channel
        while len(event_queue) > 0:
            event = event_queue.pop()
            if event.type == 'PushEvent':
                LOG.info('[GitHub] Sending new PushEvent...')
                await self.client.send_message(self.github_chan, self.format_push_event(event))

            elif event.type == 'IssuesEvent':
                LOG.info('[GitHub] Sending new IssuesEvent...')
                await self.client.send_message(self.github_chan, self.format_issue_event(event))

            elif event.type == 'PullRequestEvent':
                pass

        self.last_github_event = repo.iter_events(number=1).next().id


    ##------------------------------------

    @staticmethod
    def format_push_event(event):
        """Takes a GitHub PushEvent and returns a markdown-formatted message
        that can be relayed to the Discord channel."""

        push_message = 'New Push to branch `{}` by **{}**:\r\n'.format(
            event.payload['ref'].replace('refs/heads/', ''), event.actor.login)

        for com in event.payload['commits']:
            desc = '\nCommit `{}` by `{}`:\n'.format(com['sha'], com['author']['name'])
            desc += '```\n{}```\n'.format(com['message'])
            desc += 'https://github.com/amici-ursi/ImagesOfNetwork/commit/{}'.format(com['sha'])
            #desc += '\n---\n'
            push_message += desc

        push_message += '\r\n---'
        return push_message


    ##------------------------------------

    @staticmethod
    def format_issue_event(event):
        """Takes a GitHub IssuesEvent and returns a markdown-formatted message
        that can be relayed to the Discord channel."""

        action = event.payload['action']
        if action in ISSUE_ACTION_FILTER:

            title = event.payload['issue'].title
            user = event.actor.login
            url = event.payload['issue'].html_url

            desc = 'GitHub Issue __{}__ by **{}**:\n```\n{}\n```\r'.format(action, user, title)
            desc += '\n**Link**: {}\n'.format(url)

            return desc


    ##------------------------------------

    @staticmethod
    def format_pull_request_event(event):
        """
        If the action is "closed" and the merged key is false, the pull request was closed
        with unmerged commits.
        If the action is "closed" and the merged key is true, the pull request was merged.
        """

        pass


    ## ======================================================

    async def _process_network_modlog(self, multi):
        action_queue = deque(maxlen=25)
        url = 'https://www.reddit.com/user/{}/m/{}/about/log'.format(
            settings.MULTI_OWNER, multi)

        if self.last_modlog_action.get(multi, None) is None:
            limit = 100
        else:
            limit = round(25 * RUN_INTERVAL)

        LOG.debug('[ModLog] Getting %s modlog: limit=%s place_holder=%s',
                  multi, limit, self.last_modlog_action.get(multi, None))

        content = self.reddit.get_content(url, limit=limit,
                                          place_holder=self.last_modlog_action.get(multi, None))

        modlog = list(content)

        LOG.info('[ModLog] Processing %s %s modlog actions...', len(modlog), multi)
        self.count_modlog += len(modlog)

        for entry in [e for e in modlog if e.action in MODLOG_ACTIONS]:

            if entry.id == self.last_modlog_action.get(multi, None):
                LOG.debug('[ModLog] Found previous %s modlog placeholder entry.', multi)
                break

            else:
                action_queue.append(entry)

        while len(action_queue) > 0:
            entry = action_queue.pop()
            await self._announce_mod_action(entry)

        self.last_modlog_action[multi] = modlog[0].id
        LOG.debug('[ModLog] Finished processing %s modlog.', multi)


    ##------------------------------------

    async def _announce_mod_action(self, entry):
        mod_action = entry.action
        mod = '/u/{}'.format(entry.mod)
        sub = '/r/{}'.format(entry.subreddit.display_name)
        target = '/u/{}'.format(entry.target_author)

        message = '__*{} Moderator Update*__:\r\n'.format(settings.NETWORK_NAME)
        message += '```\n{} has '.format(mod)

        if mod_action == 'invitemoderator':
            message += 'invited {} to be a moderator'.format(target)

        elif mod_action == 'acceptmoderatorinvite':
            message += 'accepted a moderator invite'

        elif mod_action == 'removemoderator':
            message += 'removed {} as a moderator'.format(target)

        message += ' for {}'.format(sub)
        message += '\n```\r\n '

        LOG.info('[ModLog] Announcing modlog moderator %s action', entry.action)
        await self.client.send_message(self.mod_chan, message)

    ##------------------------------------

    async def _report_client_stats(self):
        while True:
            await asyncio.sleep(60 * STATS_INTERVAL)

            msg = 'Messages: **{}**\n'.format(self.count_messages) \
                + 'Multireddit posts: **{}**\n'.format(self.count_oc) \
                + 'GitHub Events: **{}**\n'.format(self.count_gh_events) \
                + 'Network Modlog Actions: **{}**\r\n'.format(self.count_modlog)

            self.count_gh_events = 0
            self.count_messages = 0
            self.count_modlog = 0
            self.count_oc = 0

            await self.client.send_message(self.stats_chan, msg)

    ##-------------------------------------

    async def _process_messages(self):
        LOG.debug('[Inbox] Checking for new messages...')
        inbox = list(self.reddit.get_unread(limit=None))
        LOG.info('[Inbox] Unread messages: %s', len(inbox))
        for message in inbox:
            await self._relay_inbox_message(message)

    ##------------------------------------

    async def _run_loop(self):
        while True:
            await self._run_once()
            LOG.info('Sleeping for %s minute(s)...', RUN_INTERVAL)
            await asyncio.sleep(60 * RUN_INTERVAL)


    async def _run_once(self):
        try:
            await self._process_messages()
            await self._process_oc_stream()
            await self._process_github_events()
            for multi in settings.MULTIREDDITS:
                await self._process_network_modlog(multi)

        except HTTPException as ex:
            LOG.error('%s: %s', type(ex), ex)
        except requests.ReadTimeout as ex:
            LOG.error('%s: %s', type(ex), ex)
        except requests.ConnectionError as ex:
            LOG.error('%s: %s', type(ex), ex)
        else:
            LOG.debug('All tasks processed.')

    ## ======================================================

    async def on_ready(self):
        """Event that fires once the Discord client has connected to Discord, logged in,
        and is ready to process new commands/events."""

        LOG.info('[Discord] Logged in as %s', self.client.user.name)

        self.inbox_chan = self.client.get_channel(settings.DISCORD_INBOX_CHAN_ID)
        self.falsepos_chan = self.client.get_channel(settings.DISCORD_FALSEPOS_CHAN_ID)
        self.github_chan = self.client.get_channel(settings.DISCORD_GITHUB_CHAN_ID)
        self.oc_chan = self.client.get_channel(settings.DISCORD_OC_CHAN_ID)
        self.mod_chan = self.client.get_channel(settings.DISCORD_MOD_CHAN_ID)
        self.stats_chan = self.client.get_channel(settings.DISCORD_STATS_CHAN_ID)

        asyncio.ensure_future(self._report_client_stats(), loop=self.client.loop)

        await self.client.send_message(self.stats_chan, 'Ready : {}'.format(datetime.datetime.now()))

        if self.run_init:
            self.run_init = False
            await self._run_loop()
            LOG.warning("Thread returning from 'await self._run_loop'!")
            self.run_init = True

    ##------------------------------------

    def run(self):
        """Initialize the Discord Bot and begin its processessing loop."""

        while True:
            try:
                LOG.info('[Discord] Starting Discord client...')
                self.client.run(settings.DISCORD_TOKEN)
            except RuntimeError as ex:
                LOG.error('%s: %s', type(ex), ex, exc_info=ex)
            else:
                LOG.warning("Thread returned from 'client.run()' blocking call!")
                asyncio.sleep(30)

            self._setup_client()


