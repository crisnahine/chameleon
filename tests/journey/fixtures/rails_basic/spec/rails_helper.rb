require "spec_helper"
ENV["RAILS_ENV"] ||= "test"
require_relative "../config/application"
require "rspec/rails"
require "factory_bot_rails"

RSpec.configure do |config|
  config.use_transactional_fixtures = true
  config.include FactoryBot::Syntax::Methods
end
