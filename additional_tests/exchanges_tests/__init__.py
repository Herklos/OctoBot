#  This file is part of OctoBot (https://github.com/Drakkar-Software/OctoBot)
#  Copyright (c) 2023 Drakkar-Software, All rights reserved.
#
#  OctoBot is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either
#  version 3.0 of the License, or (at your option) any later version.
#
#  OctoBot is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  General Public License for more details.
#
#  You should have received a copy of the GNU General Public
#  License along with OctoBot. If not, see <https://www.gnu.org/licenses/>.
import contextlib
import os
import dotenv
import mock
import time
import asyncio

import trading_backend
import octobot_commons.constants as commons_constants
import octobot_commons.asyncio_tools as asyncio_tools
import octobot_commons.os_util as os_util
import octobot_commons.configuration as configuration
import octobot_commons.tests.test_config as test_config
import octobot_trading.api as trading_api
import octobot_trading.exchanges as exchanges
import octobot_trading.constants as trading_constants
import octobot_trading.errors as errors
import octobot_trading.exchange_channel as exchange_channel
import octobot_trading.personal_data as personal_data
import octobot_tentacles_manager.constants as tentacles_manager_constants
import tests.test_utils.config as test_utils_config


LOADED_EXCHANGE_CREDS_ENV_VARIABLES = False


class NoProvidedCredentialsError(Exception):
    pass


class ExchangeChannelMock:
    def __init__(self, exchange_manager, name):
        self.exchange_manager = exchange_manager
        self.name = name
        self.consumers = []

        def _clear_pending_state(order, *args, **kwargs):
            if order.is_pending_creation():
                order.state = personal_data.OpenOrderState(order, True)

        self.get_internal_producer = mock.Mock(
            return_value=mock.Mock(
                update_order_from_exchange=mock.AsyncMock(side_effect=_clear_pending_state),
                send=mock.AsyncMock(),
            )
        )
        self.get_consumers = mock.Mock(return_value=[mock.Mock()])
        self.stop = mock.AsyncMock()
        self.flush = mock.Mock()

    def get_name(self):
        return self.name


@contextlib.asynccontextmanager
async def get_authenticated_exchange_manager(
    exchange_name, exchange_tentacle_name, config=None,
    credentials_exchange_name=None, market_filter=None,
    use_invalid_creds=False, http_proxy_callback_factory=None
):
    credentials_exchange_name = credentials_exchange_name or exchange_name
    _load_exchange_creds_env_variables_if_necessary()
    config = {**test_config.load_test_config(), **config} if config else test_config.load_test_config()
    if exchange_name not in config[commons_constants.CONFIG_EXCHANGES]:
        config[commons_constants.CONFIG_EXCHANGES][exchange_name] = {}
    config[commons_constants.CONFIG_EXCHANGES][exchange_name].update(_get_exchange_auth_details(
        credentials_exchange_name, use_invalid_creds
    ))
    exchange_type = config[commons_constants.CONFIG_EXCHANGES][exchange_name].get(
        commons_constants.CONFIG_EXCHANGE_TYPE, exchanges.get_default_exchange_type(exchange_name))
    exchange_builder = trading_api.create_exchange_builder(config, exchange_name) \
        .has_matrix("") \
        .use_tentacles_setup_config(get_tentacles_setup_config_with_exchange(exchange_tentacle_name)) \
        .set_bot_id("") \
        .is_real() \
        .is_checking_credentials(False) \
        .is_sandboxed(_get_exchange_is_sandboxed(credentials_exchange_name)) \
        .is_using_exchange_type(exchange_type) \
        .use_market_filter(market_filter) \
        .enable_storage(False) \
        .disable_trading_mode() \
        .is_exchange_only()
    if http_proxy_callback_factory:
        proxy_callback = http_proxy_callback_factory(exchange_builder.exchange_manager)
        exchange_builder.set_proxy_config(exchanges.ProxyConfig(http_proxy_callback=proxy_callback))
    exchange_manager_instance = await exchange_builder.build()
    # create trader afterwards to init exchange personal data
    exchange_manager_instance.trader.is_enabled = True
    await exchange_manager_instance.register_trader(exchange_manager_instance.trader)
    exchange_manager_instance.exchange_backend = trading_backend.exchange_factory.create_exchange_backend(
        exchange_manager_instance.exchange
    )
    exchange_manager_instance.exchange.__class__.PRINT_DEBUG_LOGS = True
    set_mocked_required_channels(exchange_manager_instance)
    t0 = time.time()
    try:
        yield exchange_manager_instance
    except errors.UnreachableExchange as err:
        raise errors.UnreachableExchange(f"{exchange_name} can't be reached, it is either offline or you are not connected "
                                         "to the internet (or a proxy is preventing connecting to this exchange).") \
            from err
    finally:
        if time.time() - t0 < 1:
            # Stopping the exchange manager too quickly can end in infinite loop, ensuring at least 1s passed
            await asyncio.sleep(1)
        await exchange_manager_instance.stop()
        trading_api.cancel_ccxt_throttle_task()
        # let updaters gracefully shutdown
        await asyncio_tools.wait_asyncio_next_cycle()


def set_mocked_required_channels(exchange_manager):
    # disable waiting time as order refresh is mocked
    personal_data.State.PENDING_REFRESH_INTERVAL = 0
    for channel in (trading_constants.ORDERS_CHANNEL, trading_constants.BALANCE_CHANNEL):
        exchange_channel.set_chan(ExchangeChannelMock(exchange_manager, channel), channel)


def get_tentacles_setup_config_with_exchange(exchange_tentacle_name):
    setup_config = test_utils_config.load_test_tentacles_config()
    setup_config.tentacles_activation[tentacles_manager_constants.TENTACLES_TRADING_PATH][exchange_tentacle_name] = True
    return setup_config


def _load_exchange_creds_env_variables_if_necessary():
    global LOADED_EXCHANGE_CREDS_ENV_VARIABLES
    if not LOADED_EXCHANGE_CREDS_ENV_VARIABLES:
        # load environment variables from .env file if exists
        dotenv_path = os.getenv("EXCHANGE_TESTS_DOTENV_PATH", os.path.dirname(os.path.abspath(__file__)))
        dotenv.load_dotenv(os.path.join(dotenv_path, ".env"), verbose=False)
        LOADED_EXCHANGE_CREDS_ENV_VARIABLES = True


def _get_exchange_auth_details(exchange_name, use_invalid_creds):
    config = {
        commons_constants.CONFIG_EXCHANGE_KEY:
            _get_exchange_credential_from_env(exchange_name, commons_constants.CONFIG_EXCHANGE_KEY),
        commons_constants.CONFIG_EXCHANGE_SECRET: _get_exchange_credential_from_env(
            exchange_name, commons_constants.CONFIG_EXCHANGE_SECRET
        ),
        commons_constants.CONFIG_EXCHANGE_PASSWORD:
            _get_exchange_credential_from_env(exchange_name, commons_constants.CONFIG_EXCHANGE_PASSWORD),
    }
    if use_invalid_creds:
        _invalidate(config)
    if not config[commons_constants.CONFIG_EXCHANGE_KEY]:
        raise NoProvidedCredentialsError(f"No {exchange_name} credentials in environment variables")
    return config


def _invalidate(exchange_config: dict):
    # try to invalidate key while returning a "plausible" value to avoid signature / parsing issues
    decoded = configuration.decrypt_element_if_possible(
        commons_constants.CONFIG_EXCHANGE_KEY, exchange_config, None
    )
    updated_decoded = decoded
    for numb in range(9):
        if str(numb) in decoded:
            # replace from the end first: coinbase-like api keys have a prefix that can be changed and remain valid
            number_rindex = decoded.rindex(str(numb))
            if number_rindex != len(decoded) - 1:
                updated_decoded = f"{decoded[:number_rindex]}{numb + 1}{decoded[number_rindex+1:]}"
                break
    if updated_decoded == decoded:
        raise ValueError("No number to invalid api key")
    exchange_config[commons_constants.CONFIG_EXCHANGE_KEY] = configuration.encrypt(updated_decoded).decode()


def _get_exchange_credential_from_env(exchange_name, cred_suffix):
    # for bybit api key: get BYBIT_KEY (as encrypted value)
    # for bybit api password: get BYBIT_PASSWORD (as encrypted value)
    # for bybit api secret: get BYBIT_SECRET (as encrypted value)
    return os.getenv(f"{exchange_name}_{cred_suffix.split('-')[-1]}".upper(), None)


def _get_exchange_is_sandboxed(exchange_name):
    # for bybit api secret: get BYBIT_SANDBOXED (as true / false)
    return os_util.parse_boolean_environment_var(f"{exchange_name}_SANDBOXED".upper(), "false")
