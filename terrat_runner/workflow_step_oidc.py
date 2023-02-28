import datetime
import json
import logging
import os
import subprocess
import time

import requests

import requests_retry
import retry
import workflow


TRIES = 3
INITIAL_SLEEP = 2
BACKOFF = 1.5

DEFAULT_AWS_AUDIENCE = 'sts.amazonaws.com'
DEFAULT_DURATION = 3600
DEFAULT_REGION = 'us-east-1'
DEFAULT_SESSION_NAME = 'terrateam'

REQUEST_URL_VAR = 'ACTIONS_ID_TOKEN_REQUEST_URL'
REQUEST_TOKEN_VAR = 'ACTIONS_ID_TOKEN_REQUEST_TOKEN'


class Auth_error(Exception):
    pass


def assume_role(state, config, web_identity_token):
    role_arn = config['role_arn']
    assume_role_arn = config.get('assume_role_arn', role_arn)
    duration = config.get('duration', DEFAULT_DURATION)
    session_name = config.get('session_name', DEFAULT_SESSION_NAME)

    proc = retry.run(
        lambda: subprocess.run(
            [
                'aws',
                'sts',
                'assume-role-with-web-identity',
                '--role-arn', assume_role_arn,
                '--role-session-name', session_name,
                '--web-identity-token', web_identity_token,
                '--duration-seconds', str(duration),
                '--output', 'json'
            ],
            cwd=state.working_dir,
            env=state.env,
            capture_output=True
        ),
        retry.finite_tries(TRIES, lambda ret: ret.returncode == 0),
        retry.betwixt_sleep_with_backoff(INITIAL_SLEEP, BACKOFF))

    if proc.returncode == 0:
        output = json.loads(proc.stdout.decode('utf-8'))
        env = state.env.copy()
        env['AWS_ACCESS_KEY_ID'] = output['Credentials']['AccessKeyId']
        env['AWS_SECRET_ACCESS_KEY'] = output['Credentials']['SecretAccessKey']
        env['AWS_SESSION_TOKEN'] = output['Credentials']['SessionToken']
        state.run_time.set_secret(env['AWS_ACCESS_KEY_ID'])
        state.run_time.set_secret(env['AWS_SECRET_ACCESS_KEY'])
        state.run_time.set_secret(env['AWS_SESSION_TOKEN'])
        state = state._replace(env=env)

        return workflow.Result(failed=False,
                               state=state,
                               workflow_step={'type': 'oidc'},
                               outputs=None)
    else:
        logging.error('OIDC : %s : ERROR : %s', role_arn, output)
        return workflow.Result(failed=True,
                               state=state,
                               workflow_step={'type': 'oidc'},
                               outputs={
                                   'text': proc.stderr.decode('utf-8')
                               })


def run_aws(state, config):
    role_arn = config['role_arn']
    audience = config.get('audience', DEFAULT_AWS_AUDIENCE)

    request_url = state.env[REQUEST_URL_VAR]
    request_token = state.env[REQUEST_TOKEN_VAR]

    res = requests_retry.get(request_url,
                             headers={
                                 'authorization': 'bearer {}'.format(request_token)
                             },
                             params={
                                 'audience': audience
                             })

    if res.status_code == 200:
        logging.info('OIDC : %s : SUCCESS', role_arn)
        web_identity_token = res.json()['value']

        state.run_time.set_secret(web_identity_token)

        web_identity_token_file = os.path.join(state.env['TERRATEAM_TMPDIR'], 'aws_oidc_token_file')
        with open(web_identity_token_file, 'w') as f:
            f.write(web_identity_token)

        region = config.get('region', DEFAULT_REGION)

        env = state.env.copy()
        env['AWS_ROLE_ARN'] = role_arn
        env['AWS_REGION'] = region
        env['AWS_WEB_IDENTITY_TOKEN_FILE'] = web_identity_token_file
        state = state._replace(env=env)
        if config.get('assume_role_enabled', True):
            logging.info('OIDC : %s : ASSUMING_ROLE', role_arn)
            return assume_role(state, config, web_identity_token)
        else:
            return workflow.Result(failed=False,
                                   state=state,
                                   workflow_step={'type': 'oidc'},
                                   outputs=None)

    else:
        logging.error('OIDC : %s : ERROR : %s',
                      role_arn,
                      res.content.decode('utf-8'))
        return workflow.Result(failed=True,
                               state=state,
                               workflow_step={'type': 'oidc'},
                               outputs={
                                   'text': res.content.decode('utf-8')
                               })


def build_domain_wide_deligation_jwt(service_account, access_token_subject, lifetime):
    now = int(time.time())
    body = {
        'iss': service_account,
        'aud': 'https://oauth2.googleapis.com/token',
        'iat': now,
        'exp': now + lifetime
    }

    if access_token_subject and access_token_subject.trim():
        body['sub'] = access_token_subject

    return json.dumps(body)


def sign_jwt(service_account, web_identity_token, unsigned_jwt):
    url = 'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{service_account}:signJwt'.format(
        service_account=service_account)
    data = {'payload': unsigned_jwt}
    headers = {
      'Accept': 'application/json',
      'Authorization': 'Bearer ' + web_identity_token,
      'Content-Type': 'application/json',
    }

    ret = requests_retry.post(url, headers=headers, json=data)

    if ret.status_code == 200:
        return ret.json()['signedJwt']
    else:
        raise Auth_error(ret.content.decode('utf-8'))


def google_oauth_token(assertion):
    url = 'https://oauth2.googleapis.com/token'
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded',
    }
    params = {
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': assertion
    }

    ret = requests_retry.post(url, headers=headers, params=params)

    if ret.status_code == 200:
        data = ret.json()
        expiration = time.time() + data['expires_in']
        return {
            'access_token': data['access_token'],
            'expiration': datetime.datetime.fromtimestamp(expiration).isoformat()
        }
    else:
        raise Auth_error(ret.content.decode('utf-8'))


def get_auth_token(provider_id, web_identity_token):
    url = 'https://sts.googleapis.com/v1/token'
    data = {
        'audience': '//iam.googleapis.com/' + provider_id,
        'grantType': 'urn:ietf:params:oauth:grant-type:token-exchange',
        'requestedTokenType': 'urn:ietf:params:oauth:token-type:access_token',
        'scope': 'https://www.googleapis.com/auth/cloud-platform',
        'subjectTokenType': 'urn:ietf:params:oauth:token-type:jwt',
        'subjectToken': web_identity_token,
    }
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }

    ret = requests_retry.post(url, headers=headers, json=data)
    if ret.status_code == 200:
        return ret.json()['access_token']
    else:
        raise Auth_error(ret.content.decode('utf-8'))


def google_access_token(token, service_account, lifetime, access_token_scopes):
    url = ('https://iamcredentials.googleapis.com/v1/projects/'
           '-/serviceAccounts/{service_account}:generateAccessToken').format(
               service_account=requests.utils.quote(service_account))
    data = {
        'lifetime': '{}s'.format(lifetime),
        'delegates': [],
        'scope': access_token_scopes
    }
    headers = {
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }

    logging.info('data %s', json.dumps(data, indent=2))

    ret = requests_retry.post(url, headers=headers, json=data)
    if ret.status_code == 200:
        data = ret.json()
        return {
            'access_token': data['accessToken'],
            'expiration': data['expireTime']
        }
    else:
        raise Auth_error(ret.content.decode('utf-8'))


def create_token(web_identity_token,
                 provider_id,
                 service_account,
                 access_token_subject,
                 lifetime,
                 access_token_scopes):
    if access_token_subject:
        unsigned_jwt = build_domain_wide_deligation_jwt(service_account,
                                                        access_token_subject,
                                                        lifetime)
        signed_jwt = sign_jwt(service_account=service_account,
                              web_identity_token=web_identity_token,
                              unsigned_jwt=unsigned_jwt)
        oauth_token_data = google_oauth_token(signed_jwt)
    else:
        auth_token = get_auth_token(provider_id, web_identity_token)
        oauth_token_data = google_access_token(auth_token,
                                               service_account,
                                               lifetime,
                                               access_token_scopes)

    return oauth_token_data


def run_gcp(state, config):
    service_account = config['service_account']
    workload_identity_provider = config['workload_identity_provider']
    access_token_lifetime = config.get('access_token_lifetime', 3600)
    audience = config.get('audience', 'https://iam.googleapis.com/' + workload_identity_provider)
    access_token_subject = config.get('access_token_subject')
    access_token_scopes = config.get('access_token_scopes',
                                     ['https://www.googleapis.com/auth/cloud-platform'])

    request_url = state.env[REQUEST_URL_VAR]
    request_token = state.env[REQUEST_TOKEN_VAR]

    res = requests_retry.get(request_url,
                             headers={
                                 'authorization': 'bearer {}'.format(request_token)
                             },
                             params={
                                 'audience': audience
                             })

    if res.status_code == 200:
        logging.info('OIDC : gcp : SUCCESS')
        web_identity_token = res.json()['value']

        state.run_time.set_secret(web_identity_token)

        try:
            oauth_token_data = create_token(web_identity_token=web_identity_token,
                                            provider_id=workload_identity_provider,
                                            service_account=service_account,
                                            access_token_subject=access_token_subject,
                                            lifetime=access_token_lifetime,
                                            access_token_scopes=access_token_scopes)

            google_oauth_access_token = oauth_token_data['access_token']
            state.run_time.set_secret(google_oauth_access_token)

            google_oauth_access_token_file = os.path.join(state.env['TERRATEAM_TMPDIR'],
                                                          'gcp_oidc_token_file')
            with open(google_oauth_access_token_file, 'w') as f:
                f.write(google_oauth_access_token)

            env = state.env.copy()
            env['GOOGLE_OAUTH_ACCESS_TOKEN_FILE'] = google_oauth_access_token_file
            env['GOOGLE_OAUTH_ACCESS_TOKEN'] = google_oauth_access_token
            state = state._replace(env=env)

            return workflow.Result(failed=False,
                                   state=state,
                                   workflow_step={'type': 'oidc'},
                                   outputs=None)
        except Auth_error as exn:
            return workflow.Result(failed=True,
                                   state=state,
                                   workflow_step={'type': 'oidc'},
                                   outputs={'text': exn.args[0]})
    else:
        logging.error('OIDC : gcp : ERROR : %s',
                      res.content.decode('utf-8'))
        return workflow.Result(failed=True,
                               state=state,
                               workflow_step={'type': 'oidc'},
                               outputs={
                                   'text': res.content.decode('utf-8')
                               })


def run(state, config):
    provider = config.get('provider', 'aws')
    if provider == 'aws':
        return run_aws(state, config)
    elif provider == 'gcp':
        return run_gcp(state, config)
    else:
        return workflow.Result(failed=True,
                               state=state,
                               workflow_step={'type': 'oidc'},
                               outputs={
                                   'text': 'Unknown provider: {}'.config.get('provider')
                               })
