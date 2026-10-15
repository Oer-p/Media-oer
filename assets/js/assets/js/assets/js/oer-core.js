
/* OER CORE v1 — SHARED SYSTEM */
(function () {
  "use strict";

  const OER = {
    version: "1.0.0",

    get db() {
      if (!window.OER_DB) {
        throw new Error("OER_DB belum dimuat.");
      }
      return window.OER_DB;
    },

    get user() {
      return this._user || null;
    },

    _user: null,

    async initialize() {
      const session = await this.db.getSession();
      this._user = session?.user || null;

      this.emit("ready", {
        user: this._user,
        version: this.version
      });

      this.db.onAuthChange(({ session }) => {
        this._user = session?.user || null;

        this.emit("authchange", {
          user: this._user
        });
      });

      return this;
    },

    emit(name, detail = {}) {
      window.dispatchEvent(
        new CustomEvent("oer:" + name, { detail })
      );
    },

    on(name, callback) {
      const handler = event => callback(event.detail);
      window.addEventListener("oer:" + name, handler);

      return () => {
        window.removeEventListener("oer:" + name, handler);
      };
    },

    setStatus(message, type = "info") {
      const status = document.querySelector("[data-oer-status]");

      if (status) {
        status.textContent = message;
        status.dataset.type = type;
      }

      this.emit("status", { message, type });
    },

    go(path) {
      window.location.href = path;
    }
  };

  window.OER = OER;

  OER.initialize().catch(error => {
    console.error("[OER CORE]", error);
    OER.emit("error", {
      message: error.message || "Gagal menginisialisasi OER CORE."
    });
  });
})();
