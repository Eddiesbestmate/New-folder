/* Klasser - frontend configuration.
 *
 * The anon key is public by design: it ships to every browser and is safe here.
 * RLS is what protects the data. The service role key must NEVER appear in any
 * file under frontend/.
 */

const KLASSER = {
  API_BASE: 'http://localhost:8000',
  SUPABASE_URL: 'https://zglgkwhocldlvrfhikfd.supabase.co',
  SUPABASE_ANON_KEY:
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InpnbGdrd2hvY2xkbHZyZmhpa2ZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODgzOTk2NzIsImV4cCI6MjEwMzk3NTY3Mn0.-PKTIaaotkt63P7BW-oMdp_jMTlr1wTbVSXpsITHMy8',
};
