
/* OER CORE v1 — DATABASE */
(function () {
  "use strict";

  const config = window.OER_CONFIG;

  if (!config) {
    throw new Error("OER_CONFIG belum dimuat.");
  }

  if (!window.supabase) {
    throw new Error("Library Supabase belum dimuat.");
  }

  const client = window.supabase.createClient(
    config.supabaseUrl,
    config.supabasePublishableKey,
    {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true
      }
    }
  );

  window.OER_DB = Object.freeze({
    client,

    async testConnection() {
      const { error } = await client
        .from(config.tables.conversations)
        .select("id")
        .limit(1);

      if (error) throw error;
      return true;
    },

    async getSession() {
      const { data, error } = await client.auth.getSession();
      if (error) throw error;
      return data.session;
    },

    async getCurrentUser() {
      const { data, error } = await client.auth.getUser();
      if (error) throw error;
      return data.user;
    },

    onAuthChange(callback) {
      return client.auth.onAuthStateChange((event, session) => {
        callback({ event, session });
      });
    }
  });
})();
