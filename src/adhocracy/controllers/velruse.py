import logging
import random
from json import loads, dumps

from pylons import request, response, session, tmpl_context as c
from pylons.controllers.util import redirect
from pylons.i18n import _
from sqlalchemy.exc import IntegrityError
from requests import get

from adhocracy.lib.auth.authentication import allowed_login_types
from adhocracy.lib.base import BaseController
from adhocracy.lib import event
from adhocracy.lib.auth import login_user
from adhocracy.model.velruse import Velruse
from adhocracy import model
from adhocracy.model.user import User
from adhocracy.lib import helpers as h
from adhocracy.lib.exceptions import DatabaseInconsistent

log = logging.getLogger(__name__)

max_user_name_length = User.user_name.property.columns[0].type.length


def unused_user_name(preferred_user_name, recursion_depth=913):
    """
    Will find an adhocracy username which is not used
    but similiar to the given one.
    """

    if (recursion_depth < 0):
        raise "internal error: could not find any unused user names!"

    if (len(preferred_user_name) > max_user_name_length
            or preferred_user_name == ""):
        unused_user_name("user",
                         recursion_depth=recursion_depth - 1)

    if User.find_by_user_name(preferred_user_name) is None:
        return preferred_user_name
    else:
        random_digit = random.randint(0, 9)
        return unused_user_name(preferred_user_name + str(random_digit),
                                recursion_depth=recursion_depth - 1)


def update_email_trust(adhocracy_user, velruse_email):
    if adhocracy_user.email == velruse_email:
        adhocracy_user.set_email_verified()


class VelruseController(BaseController):

    def logged_in(self):
        """
        Recieves authentication messages from the velruse service.
        It will log in users or connect their adhocracy account
        to an existing velruse account.
        It also creates adhocracy accounts if it thinks the user has none.
        We expect the ssi service to provide a *verified* email address.
        If none is provided, this function will crash.
        """

        token = request.params['token']
        payload = {'format': 'json', 'token': token}
        rsp = get(h.velruse_url('/auth_info'),
                  params=payload,
                  timeout=1)
        if rsp.status_code >= 400:
            return self._failure(_('Internal server error:'
                                 'velruse service not available.'))

        auth_info = loads(rsp.content)
        log.debug('received auth_info from velruse:\n' +
                  '<pre>' + dumps(auth_info, indent=4) + '</pre>')

        if 'error' in auth_info:
            error = auth_info['error']
            if error == 'user_denied':
                return self._failure(_('Login failed: ') +
                                     _('You have to give Adhocracy permission '
                                       'to connect to your Facebook account.'),
                                     auth_info)
            else:
                return self._failure(_('Login failed: ') + auth_info['error'],
                                     auth_info)

        provider_name = auth_info['provider_name']

        if provider_name not in allowed_login_types():
            self._failure(_("Logging in with %s "
                          "is not allowed on this installation.")
                          % provider_name.capitalize(),
                          auth_info)
        else:
            profile = auth_info['profile']
            email = profile['verifiedEmail']
            display_name = profile['displayName']
            preferred_name = profile['preferredUsername'].replace('.', '_')
            user_name = unused_user_name(preferred_name)

            accounts = profile['accounts']

            if c.user:
                adhocracy_user = c.user
                velruse_user = Velruse.by_user_and_domain_first(
                    adhocracy_user,
                    accounts[0]['domain'])
            else:
                velruse_user = Velruse.find_any(accounts)

                adhocracy_user = velruse_user.user if velruse_user else None

            if velruse_user:
                domain = velruse_user.domain
                domain_user = velruse_user.domain_user
            else:
                domain = accounts[0]['domain']
                domain_user = accounts[0]['userid']

            try:
                # login right away
                if adhocracy_user and velruse_user:
                    update_email_trust(adhocracy_user, email)
                    self._login(adhocracy_user)

                # create new user in both Velruse and User and login
                elif not adhocracy_user and not velruse_user:
                        new_user = self._create(user_name,
                                                email,
                                                domain,
                                                domain_user,
                                                email_verified=True,
                                                display_name=display_name)

                        adhocracy_user, velruse_user = new_user
                        self._login(adhocracy_user)

                # create new user in Velruse for a logged in user
                elif adhocracy_user and not velruse_user:
                    self._connect(adhocracy_user, domain, domain_user,
                                  provider_name,
                                  email, email_verified=True)

                # error case
                elif not adhocracy_user and velruse_user:
                    raise DatabaseInconsistent('velruse user is not associated'
                                               ' with any adhocracy user')
                else:
                    raise Exception

            # if users are deleted and we try to create them again
            # we get an IntegrityError
            except IntegrityError:
                self._failure(_("Error"))

    def _login(self, adhocracy_user):
        """
        Log the user in and redirect him to a sane place.
        """

        assert adhocracy_user

        login_user(adhocracy_user, request, response)
        session['login_type'] = 'velruse'
        if c.instance and not adhocracy_user.is_member(c.instance):
            redirect(h.base_url("/instance/join/%s?%s" % (c.instance.key,
                                                          h.url_token())))
        redirect(h.user.post_login_url(adhocracy_user))

    def _failure(self, message, auth_info=None):
        """
        Abort a velruse authenication attempt and return to login page,
        giving an error message at the openid / velruse area.
        """

        log.info('velruse login error: ' + message)
        if auth_info:
            log.debug('<pre>' + dumps(auth_info, indent=4) + '</pre>')

        h.flash(message, 'error')
        if c.user:
            return redirect(h.entity_url(c.user, member='edit'))
        else:
            redirect("/login")

    def _create(self, user_name, email, domain, domain_user,
                email_verified=False, display_name=None):
        """
        Create a user based on data gathered from velruse.
        """

        model.meta.Session.begin(subtransactions=True)

        try:
            user = User.find_by_email(email)
            if user is None:
                user = model.User.create(user_name,
                                         email,
                                         locale=c.locale,
                                         display_name=display_name)

            if email_verified:
                user.set_email_verified()

            v = Velruse(unicode(domain), unicode(domain_user), user)
            model.meta.Session.add(v)

            model.meta.Session.commit()

            event.emit(event.T_USER_CREATE, user)
            return user, v

        except Exception as e:
            model.meta.Session.rollback()
            raise e

    def _connect(self, adhocracy_user, domain, domain_user,
                 provider_name,
                 velruse_email, email_verified=False):
        """
        Connect existing adhocracy user to velruse.
        """

        if not Velruse.find(domain, domain_user):
            velruse_user = Velruse.connect(adhocracy_user, domain, domain_user,
                                           velruse_email, email_verified)

            model.meta.Session.commit()

            h.flash(_("You successfully connected to %s."
                      % provider_name.capitalize()),
                    'success')

            redirect(h.user.post_login_url(adhocracy_user))
            return velruse_user

        else:
            h.flash(_("Your %s account is already connected."
                      % provider_name.capitalize()),
                    'error')

            redirect(h.user.post_login_url(adhocracy_user))
            return None
