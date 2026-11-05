-- Add wall_clock_minutes column to loop_rounds
ALTER TABLE loop_rounds ADD COLUMN IF NOT EXISTS wall_clock_minutes NUMERIC DEFAULT 0;