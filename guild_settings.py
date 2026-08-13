import json
import os


SETTINGS_FILE = "guild_settings.json"


class GuildSettingsStore:
    def __init__(self, file_name=SETTINGS_FILE):
        self.file_name = file_name
        self.settings = self.load()

    def load(self):
        if os.path.exists(self.file_name):
            with open(self.file_name, "r", encoding="utf-8") as file:
                return json.load(file)
        return {}

    def save(self):
        with open(self.file_name, "w", encoding="utf-8") as file:
            json.dump(self.settings, file, indent=4, ensure_ascii=False)

    def get(self, guild_id):
        return self.settings.get(str(guild_id), {})

    def set_log_channel(self, guild_id, channel_id):
        self._set_channel(guild_id, "log_channel_id", channel_id)

    def set_settlement_channel(self, guild_id, channel_id):
        self._set_channel(guild_id, "settlement_channel_id", channel_id)

    def _set_channel(self, guild_id, key, channel_id):
        guild_key = str(guild_id)
        guild_settings = self.settings.setdefault(guild_key, {})
        guild_settings[key] = int(channel_id)
        self.save()

    def get_log_channel_id(self, guild_id):
        return self.get(guild_id).get("log_channel_id")

    def get_settlement_channel_id(self, guild_id):
        return self.get(guild_id).get("settlement_channel_id")
