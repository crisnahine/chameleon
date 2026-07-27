module Users
  class DeactivateUser < ApplicationService
    def initialize(user:)
      @user = user
    end

    def call
      @user.update!(deactivated_at: Time.current)
      Result.success(user: @user)
    rescue ActiveRecord::RecordInvalid => e
      Result.failure(e.message)
    end
  end
end
