# coding: utf-8

import base64
import datetime
import jinja2
import json
import logging
import os
import packages.sparklines as sparklines
import packages.pyresto.core as pyresto
import webapp2

import customfilters
from .models import User
from helpers import daterange
from google.appengine.api import memcache
from packages.slimmer import slimmer

# Constants
MEMCACHE_EXPIRATION = 60 * 60 * 24  # 1 day in seconds
RECENT_DAYS = 10


# Request Handlers
class Handler(webapp2.RequestHandler):
    @webapp2.cached_property
    def template_provider(self):
        jinja_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(os.path.join(os.getcwd(),
                                                        'templates'))
        )
        jinja_env.filters['shortnum'] = customfilters.shortnum
        jinja_env.filters['smarttruncate'] = customfilters.smarttruncate
        return jinja_env

    def render(self, template_name, values={}, ext='.html', slim=True):
        template = self.template_provider.get_template(template_name + ext)
        output = template.render(values)
        if slim:
            output = slimmer(output, 'html')
        self.write(output)
        return output

    def write(self, string):
        self.response.write(string)


class MainHandler(Handler):
    def get(self):
        self.render('index')


class BadgeHandler(Handler):
    @staticmethod
    def reduce_commits_by_date(aggr, commit):
        date = commit.commit['committer']['date'][:10]
        aggr[date] = aggr.setdefault(date, 0) + 1
        return aggr

    @staticmethod
    def reduce_commits_by_repo(aggr, commit):
        parents = commit._get_id_dict()
        repo = parents['repo'].name
        aggr[repo] = aggr.setdefault(repo, 0) + 1
        return aggr

    def get_option(self, name, defval):
        return False if self.request.get(name, defval) == '0' else True

    def calculate_user_values(self, username):
        try:
            github_user = User.get(username)
        except pyresto.Error:
            self.response.set_status(404)  # not 100% sure but good enough
            self.render('errors/404')
            return
        except Exception as err:
            self.response.set_status(500)
            logging.error(err)
            return

        languages = User.sort_languages(github_user.language_stats)
        fork_count = sum((1 for repo in github_user.repos if repo.fork))

        today = datetime.datetime.today()
        recent_than = today - datetime.timedelta(days=RECENT_DAYS)
        own_commits = github_user.get_latest_commits(recent_than)

        commits_by_repo = reduce(self.reduce_commits_by_repo,
                                 own_commits, dict())
        if commits_by_repo:
            last_project = max(commits_by_repo, key=commits_by_repo.get)
        else:
            last_project = ''
        logging.info(commits_by_repo)
        if last_project:
            last_project_url = [repo.html_url for repo in github_user.repos
                                if repo.name == last_project][0]
        else:
            last_project_url = None

        commits_by_date = reduce(self.reduce_commits_by_date,
                                 own_commits, dict())
        range = daterange(recent_than, today)
        for d in range:
            key = unicode(d.date())
            if key not in commits_by_date:
                commits_by_date[key] = 0

        commit_data = [commits_by_date[d] for d in sorted(commits_by_date)]
        max_commits = max(commit_data)
        logging.debug('Commit data %s', str(commit_data))
        commit_sparkline = 'data:image/png;base64,' +\
                           base64.b64encode(
                               sparklines.impulse(commit_data,
                                                  below_color='SlateGray',
                                                  width=3,
                                                  dmin=0,
                                                  dmax=max(commit_data)
                               ),
                           )

        return {'user': github_user.__dict__,
                'own_repos': github_user.public_repos - fork_count,
                'fork_repos': fork_count,
                'languages': languages,
                'project_followers': github_user.project_followers -\
                                     github_user.public_repos,
                'commit_sparkline': commit_sparkline,
                'max_commits': max_commits,
                'last_project': last_project,
                'last_project_url': last_project_url,
                'days': RECENT_DAYS
        }

    def get(self, username):
        support = self.get_option('s', '0')
        analytics = self.get_option('a', '1')
        jsonp = self.request.get('callback', '')
        if jsonp:  # jsonp header should be there always
            self.response.headers.add_header('content-type',
                                             'application/javascript',
                                              charset='utf-8')

        if 'origin' in self.request.headers:  # allow CORS requests always
            self.response.headers.add_header('Access-Control-Allow-Origin', '*')

        memcache_key = '{0}?{1}sa{2}j{3}'.format(username, support,
                                                 analytics, jsonp)
        cached_data = memcache.get(memcache_key)

        if cached_data:
            return self.write(cached_data)
        else:
            memcache_data_key = '!data!{}'.format(username)
            values = json.loads(memcache.get(memcache_data_key) or '{}')
            if not values:
                # Caution, the method below may alter state.
                values = self.calculate_user_values(username)

            if not values:  # still don't have the values, something went wrong
                return

            if not memcache.set(memcache_data_key,
                                json.dumps(values), MEMCACHE_EXPIRATION):
                logging.error('Memcache set failed for user data %s', username)

            if jsonp:
                output = '{0}({1})'.format(jsonp, json.dumps(values))
                self.write(output)
            else:
                values.update({'support': support, 'analytics': analytics})
                output = self.render('badge', values)

            if not memcache.set(memcache_key, output, MEMCACHE_EXPIRATION):
                logging.error('Memcache set failed for key %s', memcache_key)


class CacheHandler(Handler):
    def get(self):
        stats = memcache.get_stats()
        self.write("<b>Cache Hits:{0[hits]}</b><br>".format(stats))
        self.write("<b>Cache Misses:{0[misses]}</b><br><br>".format(stats))

    def post(self):
        if self.request.get('flush', '0') == '1':
            self.write(unicode(memcache.flush_all()))
        else:
            self.write('Nothing to do.')