import { neon } from "@neondatabase/serverless";
import { drizzle } from "drizzle-orm/neon-http";

import * as schema from "./schema";

const url = process.env.DATABASE_URL;
if (!url) {
  throw new Error(
    "DATABASE_URL is not set. Add the Neon connection string to .env.local (local) " +
      "or the Vercel project's environment variables (deployed).",
  );
}

export const db = drizzle(neon(url), { schema });
export { schema };
